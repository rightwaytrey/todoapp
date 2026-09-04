"""GET /api/meta — what the detail sheet's pickers need (docs/api.md Meta)."""
from __future__ import annotations

from fastapi import APIRouter

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

    return {
        # The five `pa` projects first, in their canonical order, always present
        # even when empty — they are the vocabulary the rest of the pa layer
        # (roundup, digest, the +claude queue) is built on.
        "projects": PA_PROJECTS + extra,
        "tags": tags,
        "priorities": ["H", "M", "L"],
        "tz": settings.tz_name,
        "now": now_iso(),
    }
