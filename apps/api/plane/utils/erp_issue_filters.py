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


ERP_EXTERNAL_SOURCE_PREFIX = "erp:"


def is_erp_external_source(external_source):
    """Is this external reference one of ours (`erp:lead`, `erp:project`, ...)?

    The ERP gateway writes the entity a task is linked to into the
    `(external_source, external_id)` pair — `erp:` plus the entity type. Upstream
    reads that pair as "this work item IS that object in another system" and keeps
    it unique per project; for us it means "this work item BELONGS TO that entity",
    and an entity has many tasks. Callers use this to tell the two meanings apart.
    """
    return bool(external_source) and str(external_source).startswith(ERP_EXTERNAL_SOURCE_PREFIX)


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


def overdue_q():
    """The derived "overdue" predicate: deadline in the past, work item still open."""
    closed_groups = [StateGroup.COMPLETED.value, StateGroup.CANCELLED.value]
    return Q(target_date__lt=timezone.now().date()) & ~Q(state__group__in=closed_groups)


def apply_overdue(request, queryset):
    """Filter on the derived "overdue" flag.

    Overdue is not a state: a task keeps its real status when the deadline
    passes, and overdue means "has a target date in the past and is neither
    completed nor cancelled".

    This one *narrows* — it is ANDed with the state filter, which is what the
    board needs ("this column, minus the overdue ones"). For the other reading,
    where overdue is one more bucket the caller ticked alongside real states,
    see `pop_state_union`.
    """
    overdue = request.GET.get("overdue")
    if not overdue:
        return queryset

    is_overdue = overdue_q()

    if overdue.lower() in ("true", "1", "yes"):
        return queryset.filter(is_overdue)
    return queryset.exclude(is_overdue)


STATE_UNION_PARAM = "include_overdue"


def pop_state_union(request, filters):
    """Turn "state IN (...) AND overdue" into "state IN (...) OR overdue".

    The ERP status filter is a checkbox list, and "overdue" sits in it next to
    the real states. Ticking one more box has to *add* work items, never remove
    them — but overdue is not a state, so ANDing it (the default reading, see
    `apply_overdue`) made the fullest possible selection the narrowest result.

    With `include_overdue=1` the caller says the two belong to the same union.
    The state filter is taken out of `filters` (mutated in place) and handed
    back as a Q, so the caller applies it with `.filter(...)` wherever it
    applies the rest. Returns None when the caller asked for nothing.
    """
    if request.GET.get(STATE_UNION_PARAM, "").lower() not in ("true", "1", "yes"):
        return None

    state_ids = filters.pop("state__in", None)
    is_overdue = overdue_q()
    # No states alongside it — the union degenerates to plain "overdue only".
    return is_overdue if not state_ids else Q(state__in=state_ids) | is_overdue
