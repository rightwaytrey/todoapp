"""GET /api/meta — what the detail sheet's pickers need (docs/api.md Meta)."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter

from .. import prefs as store
from .. import taskwarrior as tw
from ..config import PA_PROJECTS, RESERVED_TAGS, settings
from ..serialize import now_iso

router = APIRouter(tags=["meta"])


@router.get("/meta")
async def meta():
    # One export of the whole database: `projects` is "every project name in use
    # on any task" (so a project only used by a completed task still offers
    # itself), while `tags` is pending-only. Two filters would be two locks and
    # two subprocesses for a payload the client caches anyway.
    everything = await tw.export()

    extra = sorted({t.get("project") for t in everything if t.get("project")}
                   - set(PA_PROJECTS))
    tags = sorted({tag
                   for t in everything if t.get("status") == "pending"
                   for tag in (t.get("tags") or [])
                   if tag not in RESERVED_TAGS})

    # `categories` is the same names again, but arranged the way the user
    # arranged them and carrying the number the filter chip shows (design.md
    # D9/D13). `projects` stays exactly as it was: it is what the *pickers*
    # offer, it has no order of the user's in it, and two round-4 clients are
    # still reading it.
    prefs = store.load()
    counts = Counter(t.get("project") for t in everything
                     if t.get("status") == "pending" and t.get("project"))
    hidden = set(prefs.categories.hidden)
    named = list(prefs.categories.order)
    # Anything in use that the user has never arranged goes after the arranged
    # ones, alphabetically — including categories only completed tasks use, so
    # a category does not vanish from the list the moment its last task is
    # ticked off.
    in_use = sorted({t.get("project") for t in everything if t.get("project")}
                    - set(named))
    categories = [{"name": name,
                   "count": counts.get(name, 0),
                   "hidden": name in hidden}
                  for name in named + in_use]

    return {
        # The five `pa` projects first, in their canonical order, always present
        # even when empty — they are the vocabulary the rest of the pa layer
        # (roundup, digest, the +claude queue) is built on.
        "projects": PA_PROJECTS + extra,
        "categories": categories,
        "tags": tags,
        "priorities": ["H", "M", "L"],
        "tz": settings.tz_name,
        "now": now_iso(),
    }
