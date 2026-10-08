"""GET /api/meta — what the detail sheet's pickers need (docs/api.md Meta)."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Request

from .. import prefs as store
from .. import stores
from .. import taskwarrior as tw
from ..config import PA_PROJECTS, RESERVED_TAGS, settings
from ..serialize import now_iso

router = APIRouter(tags=["meta"])


@router.get("/meta")
async def meta(request: Request):
    user = stores.caller(request)
    # One export of each of the caller's stores: `projects` is "every project
    # name in use on any task" (so a project only used by a completed task
    # still offers itself), while `tags` is pending-only. Two filters would be
    # two locks and two subprocesses for a payload the client caches anyway.
    # Round 9: the private store and, with users configured, the shared one —
    # never another user's private store.
    private = await tw.export(store=user.store)
    shared_store = stores.shared_store()
    shared_rows = await tw.export(store=shared_store) if shared_store else []
    everything = private + shared_rows
    shared_names = stores.shared_names()
    shared_set = set(shared_names)

    tags = sorted({tag
                   for t in everything if t.get("status") == "pending"
                   for tag in (t.get("tags") or [])
                   if tag not in RESERVED_TAGS})

    # `projects`: the five `pa` projects first — for the default user only;
    # they are the vocabulary of HIS pa layer (roundup, digest, the +claude
    # queue), which nobody else's tasks reach (design.md D19) — then the
    # caller's in-use names, then the shared ones (docs/api.md round 9). In
    # single-user mode the shared list is empty and this is the round-4 list.
    in_use = {t.get("project") for t in everything if t.get("project")}
    head = list(PA_PROJECTS) if user.default else []
    projects = head + sorted(in_use - set(head) - shared_set)
    projects += sorted(shared_set - set(projects))

    # `categories` is the same names again, but arranged the way the user
    # arranged them and carrying the number the filter chip shows (design.md
    # D9/D13). `projects` stays exactly as it was: it is what the *pickers*
    # offer, it has no order of the user's in it, and two round-4 clients are
    # still reading it.
    prefs = store.load(user.prefs_path)
    counts = Counter(t.get("project") for t in everything
                     if t.get("status") == "pending" and t.get("project"))
    hidden = set(prefs.categories.hidden)
    named = list(prefs.categories.order)
    # Anything in use that the user has never arranged goes after the arranged
    # ones, alphabetically — including categories only completed tasks use, so
    # a category does not vanish from the list the moment its last task is
    # ticked off. Shared categories are listed even with no task at all, from
    # categories.json: an empty shared category must survive being created.
    unplaced = sorted((in_use | shared_set) - set(named))
    categories = [{"name": name,
                   "count": counts.get(name, 0),
                   "hidden": name in hidden,
                   "shared": name in shared_set}
                  for name in named + unplaced]

    return {
        "projects": projects,
        "categories": categories,
        "tags": tags,
        "priorities": ["H", "M", "L"],
        "tz": settings.tz_name,
        "now": now_iso(),
        # Who the server thinks is calling, so Settings can say so — and
        # null in single-user mode, which is the client's signal to hide
        # Share / Make private (docs/api.md round 9).
        "user": user.name,
    }
