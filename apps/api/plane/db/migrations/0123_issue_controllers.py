import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def supervisor_to_controllers(apps, schema_editor):
    """Carry every existing supervisor over as the first controller of that task.

    Runs before the column is dropped, so nobody loses their approver.
    """
    Issue = apps.get_model("db", "Issue")
    IssueController = apps.get_model("db", "IssueController")

    rows = [
        IssueController(
            issue_id=issue.id,
            controller_id=issue.supervisor_id,
            project_id=issue.project_id,
            workspace_id=issue.workspace_id,
            created_by_id=issue.created_by_id,
        )
        for issue in Issue.objects.filter(supervisor__isnull=False)
        .only("id", "supervisor_id", "project_id", "workspace_id", "created_by_id")
        .iterator(chunk_size=500)
    ]
    IssueController.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)


def controllers_to_supervisor(apps, schema_editor):
    """Reverse: a single supervisor can only hold one person, so keep the earliest."""
    Issue = apps.get_model("db", "Issue")
    IssueController = apps.get_model("db", "IssueController")

    seen = set()
    for row in (
        IssueController.objects.filter(deleted_at__isnull=True)
        .order_by("issue_id", "created_at")
        .only("issue_id", "controller_id")
        .iterator(chunk_size=500)
    ):
        if row.issue_id in seen:
            continue
        seen.add(row.issue_id)
        Issue.objects.filter(id=row.issue_id).update(supervisor_id=row.controller_id)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("db", "0122_issue_supervisor_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="IssueController",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Last Modified At")),
                ("deleted_at", models.DateTimeField(blank=True, null=True, verbose_name="Deleted At")),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                (
                    "controller",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="issue_controller",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "issue",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="issue_controller",
                        to="db.issue",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_%(class)s",
                        to="db.project",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="workspace_%(class)s",
                        to="db.workspace",
                    ),
                ),
            ],
            options={
                "verbose_name": "Issue Controller",
                "verbose_name_plural": "Issue Controllers",
                "db_table": "issue_controllers",
                "ordering": ("-created_at",),
                "unique_together": {("issue", "controller", "deleted_at")},
            },
        ),
        migrations.AddConstraint(
            model_name="issuecontroller",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("issue", "controller"),
                name="issue_controller_unique_issue_controller_when_deleted_at_null",
            ),
        ),
        migrations.AddField(
            model_name="issue",
            name="controllers",
            field=models.ManyToManyField(
                blank=True,
                related_name="controlled_issues",
                through="db.IssueController",
                through_fields=("issue", "controller"),
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Data first, column drop after — the supervisor must survive the rename.
        migrations.RunPython(supervisor_to_controllers, controllers_to_supervisor),
        migrations.RemoveField(model_name="issue", name="supervisor"),
        migrations.RenameField(
            model_name="issue",
            old_name="requires_supervisor_approval",
            new_name="requires_approval",
        ),
    ]
