"""/api/tasks — the whole app, really (docs/api.md Tasks).

Every path parameter is a full uuid and nothing else; see taskwarrior.is_uuid
for why that check is not cosmetic.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request, Response

from .. import taskwarrior as tw
from ..config import settings
from ..errors import conflict, invalid, not_found
from ..schemas import AnnotationIn, TaskCreate, TaskPatch
from ..serialize import parse_stamp, task_out, user_tags

router = APIRouter(tags=["tasks"])

STATUSES = ("pending", "completed", "all")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _load(uuid: str) -> Dict[str, Any]:
    """The raw export dict for a uuid, or a 404 in the docs/api.md envelope."""
    if not tw.is_uuid(uuid):
        # A partial uuid or an arbitrary string would be a Taskwarrior *filter*,
        # not an identity. Refusing it here is the same answer as "no such task"
        # from the client's point of view.
        raise not_found("Not a task uuid.")
    raw = await tw.get(uuid)
    if raw is None:
        raise not_found()
    return raw


async def _decorate(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Serialise a batch, resolving `blocked` with at most one extra export."""
    blocked = await tw.blocked_uuids() if any(r.get("depends") for r in rows) else set()
    return [task_out(r, blocked) for r in rows]


async def _one(uuid: str) -> Dict[str, Any]:
    """Re-read a task after a write and serialise it. Every mutating endpoint
    answers with the server's view rather than the client's guess, which is what
    lets the phone's optimistic UI reconcile (docs/design.md D6)."""
    raw = await _load(uuid)
    return (await _decorate([raw]))[0]


def _urgency_key(t: Dict[str, Any]):
    # Deterministic ordering matters more than it looks: the ETag is a hash of
    # the body, so two responses that differ only in the order of equal-urgency
    # tasks would break the 304 the phone polls on every 30 seconds.
    return (-float(t.get("urgency") or 0.0), t.get("entry") or "", t.get("uuid") or "")


def _recent_completed(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.completed_days)
    recent = []
    for t in rows:
        end = parse_stamp(t.get("end"))
        if end and end >= cutoff:
            recent.append(t)
    recent.sort(key=lambda t: (t.get("end") or "", t.get("uuid") or ""), reverse=True)
    return recent[:settings.completed_cap]


def _etag(body: bytes) -> str:
    return '"%s"' % hashlib.sha256(body).hexdigest()[:32]


def _matches(header: Optional[str], etag: str) -> bool:
    """RFC 9110 If-None-Match: a comma list, `*`, and weak validators."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith(("W/", "w/")):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #
@router.get("/tasks")
async def list_tasks(request: Request, status: str = Query("pending")):
    if status not in STATUSES:
        raise invalid("status", "must be one of %s" % ", ".join(STATUSES))

    rows: List[Dict[str, Any]] = []
    if status in ("pending", "all"):
        # `status:pending` already excludes the recurring *templates* — those
        # carry status:recurring, and it is their instances (parent set) that
        # are pending and belong on the phone (docs/api.md List).
        pending = await tw.export("status:pending")
        pending.sort(key=_urgency_key)
        rows += pending
    if status in ("completed", "all"):
        rows += _recent_completed(await tw.export("status:completed"))

    payload = await _decorate(rows)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    etag = _etag(body)

    if _matches(request.headers.get("if-none-match"), etag):
        # No body, but keep the validator so the next poll can revalidate.
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=body, media_type="application/json",
                    headers={"ETag": etag})


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
@router.post("/tasks", status_code=201)
async def create_task(body: TaskCreate):
    attrs: List[str] = []
    # Omitted (or null) on create simply means "not set" — there is nothing to
    # clear on a task that does not exist yet.
    if body.project:
        attrs.append("project:%s" % body.project)
    if body.priority:
        attrs.append("priority:%s" % body.priority)
    if body.due:
        attrs.append("due:%s" % body.due)
    for tag in (body.tags or []):
        attrs.append("+%s" % tag)

    uuid = await tw.add(body.description, attrs)
    return await _one(uuid)


# --------------------------------------------------------------------------- #
# Read one
# --------------------------------------------------------------------------- #
@router.get("/tasks/{uuid}")
async def get_task(uuid: str):
    return await _one(uuid)


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #
@router.patch("/tasks/{uuid}")
async def patch_task(uuid: str, body: TaskPatch):
    raw = await _load(uuid)
    sent = body.model_fields_set          # present-and-null != absent

    attrs: List[str] = []
    if "project" in sent:
        attrs.append("project:%s" % (body.project or ""))     # empty clears
    if "priority" in sent:
        attrs.append("priority:%s" % (body.priority or ""))
    if "due" in sent:
        attrs.append("due:%s" % (body.due or ""))
    if "tags" in sent:
        # `tags` is the full replacement set of *user* tags. Diffing rather than
        # clearing keeps `pa`'s today/overdue/due out of it entirely: they are
        # not in `current`, so they are never in a `-tag` op (docs/api.md).
        current = set(user_tags(raw))
        desired = set(body.tags or [])
        attrs += ["+%s" % t for t in sorted(desired - current)]
        attrs += ["-%s" % t for t in sorted(current - desired)]

    description = body.description if "description" in sent else None
    await tw.modify(uuid, attrs, description)
    return await _one(uuid)


# --------------------------------------------------------------------------- #
# Complete / un-complete
# --------------------------------------------------------------------------- #
@router.post("/tasks/{uuid}/done")
async def done_task(uuid: str):
    raw = await _load(uuid)
    if raw.get("status") != "completed":
        # Idempotent on purpose: the phone replays a queued write after coming
        # back on the tailnet (docs/design.md D6), and a duplicate `done` should
        # settle rather than pop an error toast. `task done` on an already-
        # completed task exits 1, so it is skipped rather than handled.
        await tw.done(uuid)
    return await _one(uuid)


@router.post("/tasks/{uuid}/undone")
async def undone_task(uuid: str):
    raw = await _load(uuid)
    if raw.get("status") != "completed":
        raise conflict("Task is %s, not completed." % raw.get("status"))
    await tw.undone(uuid)
    return await _one(uuid)


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #
@router.delete("/tasks/{uuid}", status_code=204)
async def delete_task(uuid: str):
    raw = await _load(uuid)
    if raw.get("status") != "deleted":
        await tw.delete(uuid)
    # Taskwarrior keeps the record (status:deleted), so GET still answers 200.
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Annotate
# --------------------------------------------------------------------------- #
@router.post("/tasks/{uuid}/annotations")
async def annotate_task(uuid: str, body: AnnotationIn):
    await _load(uuid)
    await tw.annotate(uuid, body.text)
    return await _one(uuid)
