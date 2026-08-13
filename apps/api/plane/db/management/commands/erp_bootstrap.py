"""Idempotent ERP bootstrap, run from the api entrypoint on every start.

Ensures the Plane objects the ERP gateway depends on exist (bot user, workspace,
API token, project + default states) and provisions employee users + pushes the
ERP<->Plane user mapping to UserService. Safe to run repeatedly — everything is
get-or-create; nothing is recreated when it already exists in the erp_plane db.

Config (env):
  ERP_WORKSPACE_SLUG       default "erp"
  ERP_BOT_EMAIL            default "bot@erp.local"
  ERP_GATEWAY_TOKEN        if set, the API token gets this exact value
                            (so leadservice Plane:API_TOKEN is stable); else random
  ERP_PROJECT_IDENTIFIER   default "TASK"
  ERP_PROJECT_NAME         default "ERP Tasks"
  USER_SERVICE_URL         e.g. http://localhost:7237 (source of employees + mapping push)
  INTEGRATION_API_KEY      X-Api-Key for UserService /users/for-plane and /plane-map/upsert
"""

import os

import requests
from django.core.management.base import BaseCommand

from plane.db.models import (
    APIToken,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.db.models.state import ERP_STATE_SOURCE, ERP_TASK_STATES
from plane.utils.erp_provisioning import ROLE_ADMIN, ROLE_MEMBER, provision_employee


class Command(BaseCommand):
    help = "Idempotent ERP bootstrap: bot user, workspace, token, project, employees + mapping."

    def _log(self, msg):
        self.stdout.write(f"[erp_bootstrap] {msg}")

    def _sync_erp_states(self, project, workspace, bot):
        """Ensure the ERP task states exist on the project.

        Matched by `external_id` (not by name), so this also works on a project
        that was already deployed with the stock Plane states, and renaming a
        state in the Plane UI never produces a duplicate. Legacy stock states are
        left in place — they may still hold work items — and only reported.
        """
        existing = {
            s.external_id: s
            for s in State.all_state_objects.filter(project=project, external_source=ERP_STATE_SOURCE)
        }

        created = 0
        default_state = None
        for spec in ERP_TASK_STATES:
            # created in list order so State.save() hands out increasing sequences
            state = existing.get(spec["external_id"])
            if state is None:
                state = State.objects.create(
                    name=spec["name"],
                    color=spec["color"],
                    group=spec["group"],
                    project=project,
                    workspace=workspace,
                    created_by=bot,
                    external_source=ERP_STATE_SOURCE,
                    external_id=spec["external_id"],
                )
                created += 1
            elif state.group != spec["group"]:
                state.group = spec["group"]
                state.save(update_fields=["group"])
            if spec.get("default"):
                default_state = state

        # a work item created without an explicit state must land in the ERP default
        if default_state is not None:
            State.all_state_objects.filter(project=project, default=True).exclude(id=default_state.id).update(
                default=False
            )
            if not default_state.default:
                default_state.default = True
                default_state.save(update_fields=["default"])

        legacy = list(
            State.objects.filter(project=project)
            .exclude(external_source=ERP_STATE_SOURCE)
            .values_list("name", flat=True)
        )
        self._log(f"erp states: {len(ERP_TASK_STATES)} total, {created} created")
        if legacy:
            self._log(f"legacy non-ERP states left in place (remove in Plane UI if unused): {', '.join(legacy)}")

    def handle(self, *args, **options):
        try:
            self._run()
        except Exception as e:  # never abort container startup on bootstrap failure
            self.stderr.write(f"[erp_bootstrap] FAILED (non-fatal): {e}")

    def _run(self):
        slug = os.environ.get("ERP_WORKSPACE_SLUG", "erp")
        # lowercased for the same reason as employee emails — User.save() stores it that way
        bot_email = os.environ.get("ERP_BOT_EMAIL", "bot@erp.local").strip().lower()
        gateway_token = os.environ.get("ERP_GATEWAY_TOKEN")
        proj_identifier = os.environ.get("ERP_PROJECT_IDENTIFIER", "TASK")
        proj_name = os.environ.get("ERP_PROJECT_NAME", "ERP Tasks")

        # 1. bot user (API-token auth, no password login)
        bot, _ = User.objects.get_or_create(email=bot_email, defaults={"username": "erp-bot"})
        changed = False
        if not bot.is_active:
            bot.is_active = True
            changed = True
        if not bot.has_usable_password():
            pass  # already unusable
        else:
            bot.set_unusable_password()
            changed = True
        if changed:
            bot.save()

        # 2. workspace + bot membership
        ws, _ = Workspace.objects.get_or_create(slug=slug, defaults={"name": "ERP", "owner": bot})
        WorkspaceMember.objects.get_or_create(workspace=ws, member=bot, defaults={"role": ROLE_ADMIN})

        # 3. gateway API token (deterministic if ERP_GATEWAY_TOKEN is provided)
        if gateway_token:
            tok, _ = APIToken.objects.get_or_create(
                user=bot, workspace=ws, label="erp-gateway",
                defaults={"token": gateway_token},
            )
            if tok.token != gateway_token:
                tok.token = gateway_token
                tok.save(update_fields=["token"])
        else:
            tok, _ = APIToken.objects.get_or_create(user=bot, workspace=ws, label="erp-gateway")
        self._log(f"workspace={ws.slug} gateway_token_ready=true")

        # 4. project + default states (states are NOT auto-seeded on ORM create)
        project, created = Project.objects.get_or_create(
            workspace=ws, identifier=proj_identifier,
            defaults={"name": proj_name, "created_by": bot},
        )
        ProjectMember.objects.get_or_create(project=project, member=bot, defaults={"role": ROLE_ADMIN})
        self._sync_erp_states(project, ws, bot)
        self._log(f"project={project.id} identifier={project.identifier}")

        # 5. provision employees + push mapping to UserService
        user_service = os.environ.get("USER_SERVICE_URL")
        api_key = os.environ.get("INTEGRATION_API_KEY")
        if not (user_service and api_key):
            self._log("USER_SERVICE_URL / INTEGRATION_API_KEY not set — skip employee provisioning")
            return

        # Source of truth is UserService, not env: fetch active employees (bots and
        # investors excluded server-side), provision each in Plane, push the mapping back.
        try:
            resp = requests.get(
                f"{user_service}/users/for-plane",
                headers={"X-Api-Key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            employees = resp.json()
        except (requests.RequestException, ValueError) as e:
            self.stderr.write(f"[erp_bootstrap] could not fetch employees from UserService: {e}")
            return

        # Per-employee isolation: one bad row (email collision, username clash, a
        # rejected mapping push) used to raise out of the whole command, so every
        # employee after it — including everybody hired since the last deploy —
        # was silently left without a Plane user.
        provisioned = 0
        failed = 0
        for emp in employees:
            email = (emp.get("email") or "").strip().lower()
            erp_id = emp.get("erpUserId")
            if not email or not erp_id:
                continue
            try:
                provision_employee(emp, email, erp_id, ws, project, user_service, api_key)
                provisioned += 1
            except Exception as e:
                failed += 1
                self.stderr.write(f"[erp_bootstrap] provisioning failed for {email} (erp id {erp_id}): {e}")

        self._log(f"employees from UserService: {len(employees)} total, {provisioned} provisioned, {failed} failed")
