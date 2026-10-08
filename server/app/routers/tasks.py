"""/api/tasks — the whole app, really (docs/api.md Tasks).

Every path parameter is a full uuid and nothing else; see taskwarrior.is_uuid
for why that check is not cosmetic.

Since round 9 (docs/api.md "Two users, a shared store", design.md D19) every
route works on the caller's two stores: their private one and, when users are
configured, the shared one. Lists merge both; a uuid resolves private-first
then shared (`_locate`); a create or a category change lands wherever the
category says. In single-user mode there is one store and every path below is
the pre-round-9 one.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query, Request, Response

from .. import moves
from .. import prefs as prefs_store
from .. import stores
from .. import taskwarrior as tw
from ..config import settings
from ..errors import api_error, conflict, invalid, not_found
from ..schemas import AnnotationIn, TaskCreate, TaskPatch
from ..serialize import (display_sort, local_now, order_in, parse_stamp,
                         task_out, user_tags)
from ..stores import Store, User

log = logging.getLogger("taskmaster.tasks")

router = APIRouter(tags=["tasks"])

STATUSES = ("pending", "completed", "all")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def caller_stores(user: User) -> List[Store]:
    """The stores this caller sees: private first, then shared if there is one."""
    shared = stores.shared_store()
    return [user.store] + ([shared] if shared else [])


async def _load(uuid: str, store: Store) -> Dict[str, Any]:
    """The raw export dict for a uuid in one store, or a 404."""
    if not tw.is_uuid(uuid):
        # A partial uuid or an arbitrary string would be a Taskwarrior *filter*,
        # not an identity. Refusing it here is the same answer as "no such task"
        # from the client's point of view.
        raise not_found("Not a task uuid.")
    raw = await tw.get(uuid, store=store)
    if raw is None:
        raise not_found()
    return raw


async def _locate(user: User, uuid: str) -> Tuple[Store, Dict[str, Any]]:
    """-> (the store it lives in, its export dict). docs/api.md round 9.

    Private first, then shared. In neither — including a uuid that sits in
    ANOTHER user's private store — is 404, never 403: a 403 would confirm the
    task exists, which is exactly what a private store must not say.

    Found in both means a move died between its import and its purge
    (moves.py). The shared copy wins and the stale private one is purged here,
    on the spot, so the duplicate never reaches a list twice.
    """
    if not tw.is_uuid(uuid):
        raise not_found("Not a task uuid.")
    own = await tw.get(uuid, store=user.store)
    shared = stores.shared_store()
    theirs = await tw.get(uuid, store=shared) if shared else None
    if theirs is not None:
        if own is not None:
            log.warning("%s is in both %s and shared — purging the private copy",
                        uuid, user.store.name)
            await tw.remove(uuid, own.get("status"), store=user.store)
        return shared, theirs
    if own is None:
        raise not_found()
    return user.store, own


async def _decorate(rows: List[Dict[str, Any]],
                    store: Optional[Store] = None) -> List[Dict[str, Any]]:
    """Serialise a batch from one store, resolving `blocked` and the recurring
    templates with at most one extra export each — and only when a row needs
    them. (Per store: a dependency or a template is only ever looked up in the
    store the row lives in; a `depends` uuid the store cannot see reads as not
    blocked, docs/api.md round 9.)

    One `now` for the whole batch: `group` is decided against the clock, and a
    response where one row was classified at 08:59:59 and the next at 09:00:00
    would put two tasks due at 09:00 in different groups."""
    return await _decorate_pairs([(store, r) for r in rows])


async def _decorate_pairs(pairs: List[Tuple[Optional[Store], Dict[str, Any]]]
                          ) -> List[Dict[str, Any]]:
    """`_decorate` over rows from several stores, keeping the given order."""
    now = local_now()
    blocked: Dict[Any, set] = {}
    templates: Dict[Any, Dict[str, Any]] = {}
    for store in {s for s, _ in pairs}:
        rows = [r for s, r in pairs if s == store]
        blocked[store] = (await tw.blocked_uuids(store=store)
                          if any(r.get("depends") for r in rows) else set())
        templates[store] = (await tw.templates(store=store)
                            if any(r.get("parent") for r in rows) else {})
    return [task_out(r, blocked[s], templates[s], now,
                     shared=bool(s and s.shared)) for s, r in pairs]


async def _one(uuid: str, store: Optional[Store] = None) -> Dict[str, Any]:
    """Re-read a task after a write and serialise it. Every mutating endpoint
    answers with the server's view rather than the client's guess, which is what
    lets the phone's optimistic UI reconcile (docs/design.md D6)."""
    raw = await _load(uuid, store)
    return (await _decorate([raw], store))[0]


async def _export_merged(user: User, *filters: str
                         ) -> List[Tuple[Store, Dict[str, Any]]]:
    """(store, row) for every row matching `filters` in the caller's stores.

    A uuid in both (a half-finished move) is reported once, as the shared
    copy — the same answer `_locate` gives — so the phone never draws a row
    twice. Purging the stale copy is left to `_locate`: a GET does not write.
    """
    out: List[Tuple[Store, Dict[str, Any]]] = []
    for store in caller_stores(user):
        out += [(store, r) for r in await tw.export(*filters, store=store)]
    shared_uuids = {r["uuid"] for s, r in out if s.shared}
    return [(s, r) for s, r in out
            if s.shared or r["uuid"] not in shared_uuids]


def store_for_category(user: User, project: Optional[str]) -> Store:
    """A shared category goes to the shared store; anything else, including no
    category at all, is private (docs/api.md round 9 Create)."""
    shared = stores.shared_store()
    if shared and project and project in stores.shared_names():
        return shared
    return user.store


def _urgency_key(t: Dict[str, Any]):
    # Applied to the RAW export rows before serialising, so the batch is in a
    # fixed order whatever the display sort does with it afterwards.
    # Deterministic ordering matters more than it looks: the ETag is a hash of
    # the body, so two responses that differ only in the order of equal-urgency
    # tasks would break the 304 the phone polls on every 30 seconds.
    return (-float(t.get("urgency") or 0.0), t.get("entry") or "", t.get("uuid") or "")


def _recent_completed(pairs: List[Tuple[Store, Dict[str, Any]]]
                      ) -> List[Tuple[Store, Dict[str, Any]]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.completed_days)
    recent = []
    for s, t in pairs:
        end = parse_stamp(t.get("end"))
        if end and end >= cutoff:
            recent.append((s, t))
    recent.sort(key=lambda p: (p[1].get("end") or "", p[1].get("uuid") or ""),
                reverse=True)
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
    user = stores.caller(request)

    # Round 9: both of the caller's stores, merged before anything is sorted,
    # so the canonical order and the 30-day window run across the union and
    # the ETag below is over the merged body — a change in the shared store
    # is a new ETag for both users.
    pending_count = 0
    rows: List[Tuple[Store, Dict[str, Any]]] = []
    if status in ("pending", "all"):
        # `status:pending` already excludes the recurring *templates* — those
        # carry status:recurring, and it is their instances (parent set) that
        # are pending and belong on the phone (docs/api.md List).
        pending = await _export_merged(user, "status:pending")
        pending.sort(key=lambda p: _urgency_key(p[1]))
        rows += pending
        pending_count = len(pending)
    if status in ("completed", "all"):
        rows += _recent_completed(await _export_merged(user, "status:completed"))

    payload = await _decorate_pairs(rows)

    # Since round 5 the *pending* half comes out in display order rather than
    # urgency order: one implementation of the canonical order, on the side
    # that both the phone and the widget already call (docs/design.md D14).
    # The completed half keeps its own order — newest first is the only order
    # a Done list has ever wanted, and grouping it by due date would be
    # nonsense.
    if pending_count:
        mode = prefs_store.load(user.prefs_path).sort.mode
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
async def create_task(request: Request, body: TaskCreate):
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

    # The category IS the sharing decision (design.md D19): there is no
    # `shared` field to send, so there is nothing to disagree with it.
    store = store_for_category(stores.caller(request), body.project)
    uuid = await tw.add(body.description, attrs, store=store)
    return await _one(uuid, store)


# --------------------------------------------------------------------------- #
# Read one
# --------------------------------------------------------------------------- #
@router.get("/tasks/{uuid}")
async def get_task(request: Request, uuid: str):
    store, _ = await _locate(stores.caller(request), uuid)
    return await _one(uuid, store)


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #
@router.patch("/tasks/{uuid}")
async def patch_task(request: Request, uuid: str, body: TaskPatch):
    user = stores.caller(request)
    store, raw = await _locate(user, uuid)
    sent = body.model_fields_set          # present-and-null != absent

    # Round 9: a category change across the private/shared line MOVES the
    # task (moves.py) — same uuid, same annotations, so from the phone's side
    # it is an ordinary PATCH. Decided before anything is written, so the
    # `order` check below asks the store the task will end up in.
    dest = store
    if "project" in sent and stores.shared_store():
        dest = store_for_category(user, body.project)

    attrs: List[str] = []
    if "project" in sent and dest == store:
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
        if not await tw.uda_order_declared(store=dest):
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

    if dest != store:
        # The new project rides inside the imported record (moves._import_row),
        # so it is not also in `attrs`. A recurring task is refused here with
        # the 422 naming `project`, before anything has changed.
        await moves.move(raw, store, dest, project=body.project)
        store = dest
        # The tag diff computed above still holds: import keeps tags as-is.

    description = body.description if "description" in sent else None
    await tw.modify(uuid, attrs, description, store=store)

    if "recur" in sent or "until" in sent:
        spawned = await _apply_recurrence(uuid, body, sent, store)
        if spawned:
            # Turning a plain task into a repeating one makes the task itself
            # the template and hands back the instance Taskwarrior spawned from
            # it — a different uuid. The client replaces the row it had
            # (docs/api.md Recurrence).
            return spawned
    return await _one(uuid, store)


# --------------------------------------------------------------------------- #
# Recurrence (docs/api.md Recurrence — all of this verified on 3.4.2)
# --------------------------------------------------------------------------- #
async def _apply_recurrence(uuid: str, body: TaskPatch, sent: set,
                            store: Optional[Store] = None
                            ) -> Optional[Dict[str, Any]]:
    """Route `recur` / `until` to whichever task actually owns the schedule.

    Taskwarrior's model is a `status:recurring` **template** plus one pending
    **instance** at a time (`recurrence.limit=1`). The phone only ever sees
    instances, so a "make this repeat weekly" tap arrives on the wrong task and
    this is where it is redirected. Returns the spawned instance when a plain
    task was promoted into a template, otherwise None.
    """
    raw = await _load(uuid, store)                # re-read: attrs just landed
    templates = await tw.templates(store=store)
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
            await tw.delete(live_parent, store=store)
        elif is_template:
            # A template addressed directly (not something the phone can reach
            # — templates are excluded from every list). Stopping the series is
            # deleting it.
            await tw.delete(uuid, store=store)
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
    await tw.modify(target, recur_attrs, store=store)

    if target == uuid and "recur" in sent and body.recur is not None \
            and not is_template:
        return await _first_instance(uuid, store)
    return None


async def _first_instance(template_uuid: str, store: Optional[Store] = None
                          ) -> Optional[Dict[str, Any]]:
    """The pending instance Taskwarrior spawned from a freshly made template.

    Filtered in Python rather than trusting `parent:<uuid>` alone: Taskwarrior
    attribute filters are prefix matches (`project:work` also matches
    `workshop`), and while a full uuid has no ambiguity in practice, this is
    the same rule the rest of the server follows — an identity is compared, not
    filtered.
    """
    rows = [r for r in await tw.export("status:pending", store=store)
            if r.get("parent") == template_uuid]
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("due") or "", r.get("uuid") or ""))
    return (await _decorate(rows[:1], store))[0]


# --------------------------------------------------------------------------- #
# Complete / un-complete
# --------------------------------------------------------------------------- #
@router.post("/tasks/{uuid}/done")
async def done_task(request: Request, uuid: str):
    store, raw = await _locate(stores.caller(request), uuid)
    if raw.get("status") != "completed":
        # Idempotent on purpose: the phone replays a queued write after coming
        # back on the tailnet (docs/design.md D6), and a duplicate `done` should
        # settle rather than pop an error toast. `task done` on an already-
        # completed task exits 1, so it is skipped rather than handled.
        await tw.done(uuid, store=store)
    return await _one(uuid, store)


@router.post("/tasks/{uuid}/undone")
async def undone_task(request: Request, uuid: str):
    store, raw = await _locate(stores.caller(request), uuid)
    if raw.get("status") != "completed":
        raise conflict("Task is %s, not completed." % raw.get("status"))
    await tw.undone(uuid, store=store)
    return await _one(uuid, store)


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #
@router.delete("/tasks/{uuid}", status_code=204)
async def delete_task(request: Request, uuid: str):
    store, raw = await _locate(stores.caller(request), uuid)
    if raw.get("status") != "deleted":
        await tw.delete(uuid, store=store)
    # Taskwarrior keeps the record (status:deleted), so GET still answers 200.
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Annotate
# --------------------------------------------------------------------------- #
@router.post("/tasks/{uuid}/annotations")
async def annotate_task(request: Request, uuid: str, body: AnnotationIn):
    store, _ = await _locate(stores.caller(request), uuid)
    await tw.annotate(uuid, body.text, store=store)
    return await _one(uuid, store)
