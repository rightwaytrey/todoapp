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
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

from fastapi import APIRouter

from .. import prefs as store
from .. import taskwarrior as tw
from ..serialize import (display_sort, due_label, local_now, now_iso, one_line,
                         task_out)

router = APIRouter(tags=["widget"])


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

    rows = [{
        "uuid": t["uuid"],
        "text": one_line(t["description"]),
        "due": due_label(t["due"], t["group"], now),
        "overdue": t["group"] == "overdue",
        "group": t["group"],
        # Only when the pref says so: the widget has no other way to know it,
        # and it draws the category whenever this string is non-empty.
        "category": (t["project"] or "") if wp.show_category else "",
    } for t in chosen[:wp.rows.large]]

    return {
        "updated": now_iso(),
        # The FULL count behind the widget's filter, not len(rows) — it is what
        # "+N more" counts up to once a family's cap has cut the list.
        "total": len(chosen),
        # `caps`, not `rows`: `rows` is the array. The widget truncates to
        # caps.small / caps.medium for the smaller families, so changing how
        # many rows the medium widget shows is a PUT /api/prefs, not a build.
        "caps": wp.rows.model_dump(),
        "rows": rows,
    }
