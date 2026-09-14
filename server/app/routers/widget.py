"""GET /api/widget — what the home-screen widget draws, already drawn.

Before round 5 the widget fetched `/api/tasks` and did its own grouping, its
own sorting and its own date labels in Swift (docs/design.md D7/D8). That is
one more implementation of the canonical order to keep in step, and — worse —
every preference about the widget would have to be compiled into it, which
means a TestFlight build to change how many rows it shows. So the server does
all of it and the widget draws the rows it is handed (docs/design.md D14).

The response deliberately carries no `urgency`, no `priority` and no tags: a
widget row is a circle, a line of text and a due label, and everything else
would just be bytes the extension decodes and throws away on every refresh.

Round 6 adds `prefs.widget.group_by`: the same rows, either in the canonical
due order ("due", what round 5 sent) or regrouped into category runs
("category"). It is decided here for the same reason the sort is — the widget
must not have to know the user's category order to draw a header.

Round 8 adds the widget's own category picker: two more top-level keys on the
feed (`category`, the active filter; `categories`, the chips to offer) and
`POST /api/widget/category` to set the filter in one call. Same reasoning as
round 6 — the widget must not fetch `/api/prefs` just to draw its own picker,
and a WidgetKit intent gets one shot at the network, not a read then a write.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Response

from .. import prefs as store
from .. import taskwarrior as tw
from ..schemas import WidgetCategorySet
from ..serialize import (display_sort, due_label, local_now, now_iso, one_line,
                         task_out)

router = APIRouter(tags=["widget"])


def category_key(order: List[str]) -> Callable[[Dict[str, Any]], Tuple]:
    """The run order for `group_by: "category"` (docs/api.md round 6).

    The user's own `prefs.categories.order` first, then the categories they
    have never arranged, alphabetically, then the tasks with no category at
    all. Position 0 picks the band, so position 1 is only ever compared inside
    one band and never has to compare an int with a string — the same shape as
    serialize.sort_key's manual mode.

    Alphabetical folds case (`Work` belongs beside `work`, not ahead of every
    lower-case name) and carries the raw name as the tie-break, so the order is
    still total: two categories differing only in case cannot swap between two
    refreshes of the same list.
    """
    placed = {name: i for i, name in enumerate(order)}

    def key(task: Dict[str, Any]) -> Tuple:
        category = task.get("project") or ""
        if not category:
            return (2, "")
        if category in placed:
            return (0, placed[category])
        return (1, (category.lower(), category))

    return key


def category_chips(order: List[str], hidden: List[str], active: Optional[str],
                   tasks: List[Dict[str, Any]]) -> List[str]:
    """The widget's own category picker (docs/api.md round 8).

    `prefs.categories.order` minus anything hidden, then the categories any
    PENDING task carries that never made it into that order — alphabetically,
    reusing `category_key()`'s own second band so this list and the
    `group_by: "category"` run order can never drift apart. A category with
    no pending task still appears when it is in `order`: a chip that answers
    "Nothing due" is honest, and a picker that reshuffles itself as tasks
    complete is not.

    Hidden ones are dropped — the widget is a picker, not the settings
    screen, and a hidden category is one the user took out of every picker —
    except `active`, which is always offered so the lit chip can always be
    un-lit, even one hidden after being chosen, or one `POST
    /api/widget/category` pointed at a name with no task and no place in
    `order` at all (that call only checks the name's shape).
    """
    key = category_key(order)
    in_use = {t["project"] for t in tasks if t.get("project")}
    extra = sorted((c for c in in_use if c not in order),
                   key=lambda name: key({"project": name}))

    chips = [c for c in order + extra if c not in hidden or c == active]
    if active and active not in chips:
        chips.append(active)
    return chips


@router.get("/widget")
async def widget_feed():
    prefs = store.load()
    wp = prefs.widget

    raw = await tw.export("status:pending")
    templates = await tw.templates() if any(r.get("parent") for r in raw) else {}
    now = local_now()
    tasks = [task_out(r, set(), templates, now) for r in raw]

    groups = set(wp.groups)
    # The upcoming window is a DATE comparison, like everything else that
    # touches `due` (design.md D3): "within 7 days" is "on or before the date 7
    # days from today", not "within 168 hours", so a task due at 09:00 on the
    # seventh day is in and does not fall out at lunchtime.
    horizon = None
    if "upcoming" in groups:
        horizon = (now.date()
                   + timedelta(days=wp.upcoming_days)).strftime("%Y-%m-%d")

    chosen: List[Dict[str, Any]] = []
    for t in tasks:
        if t["group"] not in groups:
            continue
        if t["group"] == "upcoming" and horizon is not None \
                and (t["due"] or "")[:10] > horizon:
            continue
        if wp.category and t["project"] != wp.category:
            continue
        chosen.append(t)

    chosen = display_sort(chosen, prefs.sort.mode)

    # Round 6: `group_by: "category"` regroups the rows the widget already had.
    # A STABLE sort over the display order is the whole implementation — "and
    # within a category in the normal display order" is what the list already
    # is, so nothing re-derives the canonical key a second time.
    by_category = wp.group_by == "category"
    if by_category:
        chosen.sort(key=category_key(prefs.categories.order))

    rows = [{
        "uuid": t["uuid"],
        "text": one_line(t["description"]),
        "due": due_label(t["due"], t["group"], now),
        "overdue": t["group"] == "overdue",
        "group": t["group"],
        # Only when the pref says so: the widget has no other way to know it,
        # and it draws the category whenever this string is non-empty. Under
        # `group_by: "category"` it is always sent — the category is what the
        # rows are grouped BY, and the widget draws a header per run whether or
        # not the user also asked for it on every row.
        "category": (t["project"] or "") if (by_category or wp.show_category)
                    else "",
    } for t in chosen[:wp.rows.large]]

    return {
        "updated": now_iso(),
        # The FULL count behind the widget's filter, not len(rows) — it is what
        # "+N more" counts up to once a family's cap has cut the list.
        "total": len(chosen),
        # Echoed rather than inferred: the widget draws a category header per
        # run only in this mode, and reading prefs itself would be a second
        # request from an extension that gets one shot at the network.
        "group_by": wp.group_by,
        # Round 8: the active filter and the chips to offer for it, echoed for
        # the same one-request-one-picture reason as group_by above. `tasks`,
        # not `chosen`: the picker offers every category a PENDING task
        # carries, not only the ones surviving THIS feed's own groups/horizon
        # filter — a chip must not disappear because "today" is the only
        # enabled group.
        "category": wp.category,
        "categories": category_chips(prefs.categories.order,
                                     prefs.categories.hidden, wp.category,
                                     tasks),
        # `caps`, not `rows`: `rows` is the array. The widget truncates to
        # caps.small / caps.medium for the smaller families, so changing how
        # many rows the medium widget shows is a PUT /api/prefs, not a build.
        "caps": wp.rows.model_dump(),
        "rows": rows,
    }


@router.post("/widget/category", status_code=204)
async def set_widget_category(body: WidgetCategorySet):
    """`{"category": string|null}` → 204 (docs/api.md round 8).

    One intent, one write — not a GET-then-PUT of the whole `/api/prefs`
    document from the widget extension, which gets one shot at the network
    and would otherwise risk silently dropping a key it was never built to
    know (design.md D15's "unknown keys are dropped" then cuts the wrong
    way). Same pattern as `routers/categories.py`'s rename/delete: read the
    current `widget` section, replace just this field, write it back through
    `store.update`, which re-reads under its own lock so a `sort` or
    `categories` write landing between this read and that write is not
    clobbered.
    """
    widget = store.load().widget.model_copy(update={"category": body.category})
    store.update(widget=widget.model_dump())
    return Response(status_code=204)
