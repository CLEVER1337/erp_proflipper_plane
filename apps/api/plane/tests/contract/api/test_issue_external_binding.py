# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""ERP (task_B3): the `(external_source, external_id)` pair on a work item.

Upstream keeps the pair unique per project — it reads it as "this work item IS that
object in another system". The ERP gateway writes `erp:<entity>` into it to mean "this
work item BELONGS TO that entity" (a project, a lead, an investor, an employee), and an
entity has many tasks. Uniqueness there let exactly one task be linked to an entity and
answered every next one with a 409, which the gateway surfaced as `502
plane_update_failed`. These tests pin both halves: ours is free, foreign sources still
conflict.
"""

import pytest
from rest_framework import status

from plane.db.models import Issue, Project, ProjectMember, State


@pytest.fixture
def project(db, workspace, create_user):
    """A project with the user as admin and a default state for new work items"""
    project = Project.objects.create(
        name="Test Project",
        identifier="TP",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    State.objects.create(
        name="Todo",
        group="unstarted",
        default=True,
        project=project,
        workspace=workspace,
        created_by=create_user,
    )
    return project


def issues_url(workspace_slug, project_id):
    return f"/api/v1/workspaces/{workspace_slug}/projects/{project_id}/issues/"


def issue_url(workspace_slug, project_id, issue_id):
    return f"{issues_url(workspace_slug, project_id)}{issue_id}/"


@pytest.mark.contract
class TestErpEntityBinding:
    """An ERP entity may hold any number of work items"""

    @pytest.mark.django_db
    @pytest.mark.parametrize("external_source", ["erp:project", "erp:lead", "erp:investor", "erp:user"])
    def test_create_many_tasks_linked_to_the_same_entity(
        self, api_key_client, workspace, project, external_source
    ):
        url = issues_url(workspace.slug, project.id)
        payload = {"external_source": external_source, "external_id": "286"}

        first = api_key_client.post(url, dict(payload, name="First task"), format="json")
        second = api_key_client.post(url, dict(payload, name="Second task"), format="json")

        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_201_CREATED
        assert Issue.objects.filter(external_source=external_source, external_id="286").count() == 2

    @pytest.mark.django_db
    def test_link_task_to_entity_that_already_has_one(self, api_key_client, workspace, project):
        """The reported task_B3 repro: attach a second task to a project from the card."""
        Issue.objects.create(
            name="Task already on the project",
            project=project,
            workspace=workspace,
            external_source="erp:project",
            external_id="286",
        )
        unlinked = Issue.objects.create(name="Unlinked task", project=project, workspace=workspace)

        response = api_key_client.patch(
            issue_url(workspace.slug, project.id, unlinked.id),
            {"external_source": "erp:project", "external_id": "286"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        unlinked.refresh_from_db()
        assert unlinked.external_source == "erp:project"
        assert unlinked.external_id == "286"

    @pytest.mark.django_db
    def test_move_task_between_entities(self, api_key_client, workspace, project):
        """Re-binding from one entity to another taken one: only external_id is sent."""
        Issue.objects.create(
            name="Task on the target project",
            project=project,
            workspace=workspace,
            external_source="erp:project",
            external_id="288",
        )
        linked = Issue.objects.create(
            name="Task being moved",
            project=project,
            workspace=workspace,
            external_source="erp:project",
            external_id="286",
        )

        response = api_key_client.patch(
            issue_url(workspace.slug, project.id, linked.id),
            {"external_id": "288"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        linked.refresh_from_db()
        assert linked.external_id == "288"

    @pytest.mark.django_db
    def test_unlinking_and_relinking_stays_allowed(self, api_key_client, workspace, project):
        """`entityType: null` clears the pair; the entity can then be picked again."""
        linked = Issue.objects.create(
            name="Linked task",
            project=project,
            workspace=workspace,
            external_source="erp:project",
            external_id="286",
        )
        url = issue_url(workspace.slug, project.id, linked.id)

        cleared = api_key_client.patch(url, {"external_source": None, "external_id": None}, format="json")
        relinked = api_key_client.patch(
            url, {"external_source": "erp:project", "external_id": "286"}, format="json"
        )

        assert cleared.status_code == status.HTTP_200_OK
        assert relinked.status_code == status.HTTP_200_OK
        linked.refresh_from_db()
        assert linked.external_id == "286"


@pytest.mark.contract
class TestForeignExternalIdStillUnique:
    """Upstream behaviour is untouched for everyone else"""

    @pytest.mark.django_db
    def test_create_duplicate_foreign_pair_conflicts(self, api_key_client, workspace, project):
        Issue.objects.create(
            name="Imported work item",
            project=project,
            workspace=workspace,
            external_source="github",
            external_id="ext-123",
        )

        response = api_key_client.post(
            issues_url(workspace.slug, project.id),
            {"name": "Same work item again", "external_source": "github", "external_id": "ext-123"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "same external id" in response.data["error"]

    @pytest.mark.django_db
    def test_patch_duplicate_foreign_pair_conflicts(self, api_key_client, workspace, project):
        Issue.objects.create(
            name="Imported work item",
            project=project,
            workspace=workspace,
            external_source="github",
            external_id="ext-123",
        )
        other = Issue.objects.create(
            name="Another imported work item",
            project=project,
            workspace=workspace,
            external_source="github",
            external_id="ext-999",
        )

        response = api_key_client.patch(
            issue_url(workspace.slug, project.id, other.id),
            {"external_id": "ext-123"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        other.refresh_from_db()
        assert other.external_id == "ext-999"
