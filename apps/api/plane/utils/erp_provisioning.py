"""Provisioning of ERP employees into Plane.

Shared by two callers: the `erp_bootstrap` management command, which runs on every
api start and sweeps the whole employee list, and the internal endpoint UserService
calls the moment somebody is hired. Without the second one a new employee cannot be
given a task until Plane is restarted — the gateway answers
`assignee_{id}_not_mapped_to_plane` because the ERP<->Plane pair does not exist yet.

Everything here is get-or-create, so both callers can run in any order, repeatedly.
"""

import os
import uuid

import requests
from django.db import transaction

from plane.db.models import Project, ProjectMember, User, Workspace, WorkspaceMember

ROLE_ADMIN = 20
ROLE_MEMBER = 15


def unique_username(preferred, email):
    """A username no other Plane user holds.

    `username` is unique in Plane and unrelated to `email`, so two ERP logins
    that collide (or a login equal to somebody else's email) must not abort
    provisioning. Only used on create — an existing user keeps its username.
    """
    for candidate in (preferred, email):
        if candidate and not User.objects.filter(username=candidate).exists():
            return candidate
    while True:
        candidate = f"{email}-{uuid.uuid4().hex[:6]}"
        if not User.objects.filter(username=candidate).exists():
            return candidate


def provision_employee(emp, email, erp_id, ws, project, user_service, api_key):
    """Get-or-create one employee in Plane and push the ERP<->Plane pair back.

    Lookup is by the already-lowercased email: `User.save()` lowercases what it
    stores, so matching against the raw ERP email misses the existing row and
    turns a no-op re-run into a create that trips the username unique index.
    """
    with transaction.atomic():
        user = User.objects.filter(email=email).first()
        if user is None:
            username = unique_username(emp.get("username") or email, email)
            user = User.objects.create(email=email, username=username)
        if user.has_usable_password():
            user.set_unusable_password()
            user.save(update_fields=["password"])
        WorkspaceMember.objects.get_or_create(workspace=ws, member=user, defaults={"role": ROLE_MEMBER})
        ProjectMember.objects.get_or_create(project=project, member=user, defaults={"role": ROLE_MEMBER})

    # outside the transaction: a rejected push must not discard a correctly
    # provisioned Plane user, the next boot just re-pushes the same pair
    resp = requests.post(
        f"{user_service}/plane-map/upsert",
        headers={"X-Api-Key": api_key},
        json={"erpUserId": erp_id, "planeUserId": str(user.id), "planeEmail": email},
        timeout=10,
    )
    if not resp.ok:
        # 409 plane_user_already_mapped means two ERP users share one Plane user
        raise RuntimeError(f"mapping push returned {resp.status_code}: {resp.text.strip()[:200]}")

    return user


def erp_workspace_and_project():
    """Workspace and project the ERP tasks live in, or (None, None) before bootstrap.

    The endpoint runs against an already bootstrapped instance, so it looks the two
    up instead of creating them — creating a workspace on a hiring event would hide
    a misconfigured container behind a second, empty workspace.
    """
    ws = Workspace.objects.filter(slug=os.environ.get("ERP_WORKSPACE_SLUG", "erp")).first()
    if ws is None:
        return None, None

    project = Project.objects.filter(
        workspace=ws, identifier=os.environ.get("ERP_PROJECT_IDENTIFIER", "TASK")
    ).first()
    return ws, project
