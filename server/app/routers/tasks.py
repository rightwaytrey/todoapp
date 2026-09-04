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

from .. import prefs as prefs_store
from .. import taskwarrior as tw
from ..config import settings
from ..errors import api_error, conflict, invalid, not_found
from ..schemas import AnnotationIn, TaskCreate, TaskPatch
from ..serialize import (display_sort, local_now, order_in, parse_stamp,
                         task_out, user_tags)

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
    """Serialise a batch, resolving `blocked` and the recurring templates with
    at most one extra export each — and only when a row needs them.

    One `now` for the whole batch: `group` is decided against the clock, and a
    response where one row was classified at 08:59:59 and the next at 09:00:00
    would put two tasks due at 09:00 in different groups."""
    blocked = await tw.blocked_uuids() if any(r.get("depends") for r in rows) else set()
    templates = await tw.templates() if any(r.get("parent") for r in rows) else {}
    now = local_now()
    return [task_out(r, blocked, templates, now) for r in rows]


async def _one(uuid: str) -> Dict[str, Any]:
    """Re-read a task after a write and serialise it. Every mutating endpoint
    answers with the server's view rather than the client's guess, which is what
    lets the phone's optimistic UI reconcile (docs/design.md D6)."""
    raw = await _load(uuid)
    return (await _decorate([raw]))[0]


def _urgency_key(t: Dict[str, Any]):
    # Applied to the RAW export rows before serialising, so the batch is in a
    # fixed order whatever the display sort does with it afterwards.
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

    pending_count = 0
    rows: List[Dict[str, Any]] = []
    if status in ("pending", "all"):
        # `status:pending` already excludes the recurring *templates* — those
        # carry status:recurring, and it is their instances (parent set) that
        # are pending and belong on the phone (docs/api.md List).
        pending = await tw.export("status:pending")
        pending.sort(key=_urgency_key)
        rows += pending
        pending_count = len(pending)
    if status in ("completed", "all"):
        rows += _recent_completed(await tw.export("status:completed"))

    payload = await _decorate(rows)

    # Since round 5 the *pending* half comes out in display order rather than
    # urgency order: one implementation of the canonical order, on the side
    # that both the phone and the widget already call (docs/design.md D14).
    # The completed half keeps its own order — newest first is the only order
    # a Done list has ever wanted, and grouping it by due date would be
    # nonsense.
    if pending_count:
        mode = prefs_store.load().sort.mode
        payload = display_sort(payload[:pending_count], mode) + payload[pending_count:]

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
    if "order" in sent:
        # Checked for the CLEAR too, not just the write. With the UDA
        # undeclared, `modify order:1500` exits 0 and rewrites the task's
        # DESCRIPTION to "order:1500" — the token is not an attribute
        # Taskwarrior knows, so it falls through to the text — and `modify
        # order:` is no better: it sets the description to "order:". Verified
        # on 3.4.2. A drag would quietly shred the list, so nothing is
        # attempted until the two lines are in the taskrc.
        if not await tw.uda_order_declared():
            raise api_error(
                409, "conflict",
                "The `order` UDA is not declared in this server's .taskrc. "
                "Run server/deploy/install.sh, or add uda.order.type=numeric "
                "and uda.order.label=Order, then restart the service.")
        attrs.append("order:%s" % ("" if body.order is None
                                   else order_in(body.order)))
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

    if "recur" in sent or "until" in sent:
        spawned = await _apply_recurrence(uuid, body, sent)
        if spawned:
            # Turning a plain task into a repeating one makes the task itself
            # the template and hands back the instance Taskwarrior spawned from
            # it — a different uuid. The client replaces the row it had
            # (docs/api.md Recurrence).
            return spawned
    return await _one(uuid)


# --------------------------------------------------------------------------- #
# Recurrence (docs/api.md Recurrence — all of this verified on 3.4.2)
# --------------------------------------------------------------------------- #
async def _apply_recurrence(uuid: str, body: TaskPatch,
                            sent: set) -> Optional[Dict[str, Any]]:
    """Route `recur` / `until` to whichever task actually owns the schedule.

    Taskwarrior's model is a `status:recurring` **template** plus one pending
    **instance** at a time (`recurrence.limit=1`). The phone only ever sees
    instances, so a "make this repeat weekly" tap arrives on the wrong task and
    this is where it is redirected. Returns the spawned instance when a plain
    task was promoted into a template, otherwise None.
    """
    raw = await _load(uuid)                       # re-read: attrs just landed
    templates = await tw.templates()
    parent = raw.get("parent") or None
    live_parent = parent if parent in templates else None
    is_template = raw.get("status") == "recurring"

    # --- stop repeating -----------------------------------------------------
    if "recur" in sent and body.recur is None:
        if live_parent:
            # Delete the TEMPLATE and leave this instance exactly as it is.
            #
            # The contract asked for `modify parent: recur: imask:` on the
            # instance as well. None of it is possible on 3.4.2 and two thirds
            # of it are harmful:
            #   * `modify recur:` -> "You cannot remove the recurrence from a
            #     recurring task." (exit 2). `task import` with the field
            #     stripped hits the same check.
            #   * `modify parent:` succeeds and PROMOTES the instance to a
            #     live template, which immediately spawns a fresh instance —
            #     the row the user wanted to keep leaves the pending list and
            #     is replaced by a new one that still repeats. The exact
            #     opposite of "stop repeating".
            # Deleting the template alone is enough: the surviving instance
            # never spawns another (verified — completing it produces nothing),
            # and serialize.task_out reports a row whose template is gone as
            # the plain task it now is.
            await tw.delete(live_parent)
        elif is_template:
            # A template addressed directly (not something the phone can reach
            # — templates are excluded from every list). Stopping the series is
            # deleting it.
            await tw.delete(uuid)
        # A plain task, or an instance whose template is already gone: there is
        # nothing repeating to stop, and saying so with an error would fail a
        # replayed offline write (design.md D6).
        return None

    target = live_parent or uuid
    recur_attrs: List[str] = []

    # --- start / change repeating ------------------------------------------
    if "recur" in sent and body.recur is not None:
        if live_parent is None and not is_template:
            # Promotion. Taskwarrior refuses `recur` without a `due` ("You
            # cannot specify a recurring task without a due date.", exit 2), so
            # answer that as the 422 the contract names rather than a 502.
            if not raw.get("due"):
                raise invalid("recur", "recurrence needs a due date")
            if parent:
                # An orphan instance — its template was deleted by a previous
                # "stop repeating". `parent:`/`imask:` are what turn it back
                # into a template; without them Taskwarrior just writes `recur`
                # onto a dead instance and nothing ever spawns (verified).
                recur_attrs += ["parent:", "imask:"]
        recur_attrs.append("recur:%s" % body.recur)

    if "until" in sent:
        recur_attrs.append("until:%s" % (body.until or ""))

    if not recur_attrs:
        return None
    await tw.modify(target, recur_attrs)

    if target == uuid and "recur" in sent and body.recur is not None \
            and not is_template:
        return await _first_instance(uuid)
    return None


async def _first_instance(template_uuid: str) -> Optional[Dict[str, Any]]:
    """The pending instance Taskwarrior spawned from a freshly made template.

    Filtered in Python rather than trusting `parent:<uuid>` alone: Taskwarrior
    attribute filters are prefix matches (`project:work` also matches
    `workshop`), and while a full uuid has no ambiguity in practice, this is
    the same rule the rest of the server follows — an identity is compared, not
    filtered.
    """
    rows = [r for r in await tw.export("status:pending")
            if r.get("parent") == template_uuid]
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("due") or "", r.get("uuid") or ""))
    return (await _decorate(rows[:1]))[0]


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
