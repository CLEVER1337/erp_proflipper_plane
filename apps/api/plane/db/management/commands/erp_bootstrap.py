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
from plane.db.models.state import DEFAULT_STATES

ROLE_ADMIN = 20
ROLE_MEMBER = 15


class Command(BaseCommand):
    help = "Idempotent ERP bootstrap: bot user, workspace, token, project, employees + mapping."

    def _log(self, msg):
        self.stdout.write(f"[erp_bootstrap] {msg}")

    def handle(self, *args, **options):
        try:
            self._run()
        except Exception as e:  # never abort container startup on bootstrap failure
            self.stderr.write(f"[erp_bootstrap] FAILED (non-fatal): {e}")

    def _run(self):
        slug = os.environ.get("ERP_WORKSPACE_SLUG", "erp")
        bot_email = os.environ.get("ERP_BOT_EMAIL", "bot@erp.local")
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
        self._log(f"workspace={ws.slug} token={tok.token}")

        # 4. project + default states (states are NOT auto-seeded on ORM create)
        project, created = Project.objects.get_or_create(
            workspace=ws, identifier=proj_identifier,
            defaults={"name": proj_name, "created_by": bot},
        )
        ProjectMember.objects.get_or_create(project=project, member=bot, defaults={"role": ROLE_ADMIN})
        if not State.objects.filter(project=project).exists():
            State.objects.bulk_create([
                State(
                    name=s["name"], color=s["color"], project=project,
                    sequence=s["sequence"], workspace=ws, group=s["group"],
                    default=s.get("default", False), created_by=bot,
                )
                for s in DEFAULT_STATES
            ])
            self._log(f"seeded {len(DEFAULT_STATES)} default states")
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

        for emp in employees:
            email = emp.get("email")
            erp_id = emp.get("erpUserId")
            if not email or not erp_id:
                continue
            username = emp.get("username") or email
            u, _ = User.objects.get_or_create(email=email, defaults={"username": username})
            if u.has_usable_password():
                u.set_unusable_password()
                u.save(update_fields=["password"])
            WorkspaceMember.objects.get_or_create(workspace=ws, member=u, defaults={"role": ROLE_MEMBER})
            ProjectMember.objects.get_or_create(project=project, member=u, defaults={"role": ROLE_MEMBER})
            try:
                requests.post(
                    f"{user_service}/plane-map/upsert",
                    headers={"X-Api-Key": api_key},
                    json={"erpUserId": erp_id, "planeUserId": str(u.id), "planeEmail": email},
                    timeout=10,
                )
            except requests.RequestException as e:
                self.stderr.write(f"[erp_bootstrap] mapping push failed for {email}: {e}")

        self._log(f"provisioned {len(employees)} employees from UserService")
