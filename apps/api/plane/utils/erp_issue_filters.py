"""ERP: work item filters shared by the work item list and the goal list.

The ERP task list (`plane/api/views/issue.py`) parses a handful of filters that
upstream Plane does not know about — the external entity a task is linked to, the
controllers this fork added, the derived "overdue" flag and the "involved with"
predicate behind the `mine` / `department` scopes.

task_TT2 needs the very same parsing on the goal (module) list: a goal shows up
when at least one of its work items matches the *task* filters the user has set.
Keeping one implementation here means the two lists can never drift apart — a
filter added for tasks is understood by goals for free.
"""

# Django imports
from django.db.models import Q
from django.utils import timezone

# Module imports
from plane.db.models.state import StateGroup
from plane.utils.issue_filters import filter_valid_uuids, issue_filters


def build_erp_issue_filters(request):
    """Query-param filters for the work item list.

    Reuses the shared filter parser (state, state_group, assignees,
    created_by, priority, labels, target_date ranges, module, ...) and adds the
    ERP ones on top: the external entity a task is linked to and the controller
    fields this fork added to Issue.
    """
    filters = issue_filters(request.GET, "GET")

    external_source = request.GET.get("external_source")
    if external_source:
        filters["external_source"] = external_source

    external_id = request.GET.get("external_id")
    if external_id:
        filters["external_id"] = external_id

    controllers = request.GET.get("controllers")
    if controllers:
        controller_ids = filter_valid_uuids([item for item in controllers.split(",") if item != "null"])
        if controller_ids:
            filters["controllers__id__in"] = controller_ids

    requires_approval = request.GET.get("requires_approval")
    if requires_approval:
        filters["requires_approval"] = requires_approval.lower() in ("true", "1", "yes")

    return filters


def apply_involves(request, queryset):
    """Filter to work items a given set of users is involved with.

    "Involved" means assignee, creator or controller. The ERP needs this as one
    predicate ("my tasks", "my department's tasks") and the ordinary filters are
    ANDed together, so it cannot be expressed with `assignees` + `created_by`.
    """
    involves = request.GET.get("involves")
    if not involves:
        return queryset

    user_ids = filter_valid_uuids([item for item in involves.split(",") if item != "null"])
    if not user_ids:
        return queryset

    return queryset.filter(
        Q(assignees__id__in=user_ids) | Q(created_by_id__in=user_ids) | Q(controllers__id__in=user_ids)
    ).distinct()


def apply_overdue(request, queryset):
    """Filter on the derived "overdue" flag.

    Overdue is not a state: a task keeps its real status when the deadline
    passes, and overdue means "has a target date in the past and is neither
    completed nor cancelled".
    """
    overdue = request.GET.get("overdue")
    if not overdue:
        return queryset

    closed_groups = [StateGroup.COMPLETED.value, StateGroup.CANCELLED.value]
    is_overdue = Q(target_date__lt=timezone.now().date()) & ~Q(state__group__in=closed_groups)

    if overdue.lower() in ("true", "1", "yes"):
        return queryset.filter(is_overdue)
    return queryset.exclude(is_overdue)
