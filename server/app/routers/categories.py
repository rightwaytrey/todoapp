"""/api/categories — rename and delete a category across the whole database.

A category *is* Taskwarrior's `project` (docs/design.md D13); the word changes
only at the glass. So both endpoints are one bulk `modify`, and both of them
also rewrite the preferences, because a category the user renamed must not
leave a dead entry in the picker order or a dead `p:` chip in the filter bar.

Two Taskwarrior facts hold this together, both verified on 3.4.2 and neither
visible from the command line (see also taskwarrior.bulk_modify):

* **`project.is:`, never `project:`.** Attribute filters are PREFIX matches.
  `task project:work modify project:job` also renames `workshop` and
  `work.sub`. On this box that is silent data loss across real tasks, which is
  why every filter here is the `.is:` form.
* **`rc.bulk=0`.** `rc.confirmation=off` does not cover the bulk prompt, and
  without it a rename over three or more tasks exits 1 having changed nothing.

Round 9 (docs/api.md "Shared categories", design.md D19) adds share and
unshare, and splits rename/delete by which side of the line the name is on. A
category name means one thing on the box: it is in `categories.json`, or it is
private to whoever uses it — so every rule below is about keeping a name from
ending up on both sides. A shared rename/delete/unshare rewrites EVERY user's
prefs, because each of them may have arranged, hidden or chipped that name.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request, Response

from .. import moves
from .. import prefs as store
from .. import stores
from .. import taskwarrior as tw
from ..errors import conflict, invalid
from ..schemas import CategoryDelete, CategoryName, CategoryRename
from ..stores import User

log = logging.getLogger("taskmaster.categories")

router = APIRouter(prefix="/categories", tags=["categories"])

# Everything a task can be that is not "thrown away": pending, completed,
# waiting and the recurring templates. `status.not:deleted` is one filter for
# all four, and leaving deleted tasks alone means a category rename does not
# quietly rewrite history the user already discarded.
NOT_DELETED = "status.not:deleted"


def _rename_in(names: List[str], old: str, new: str) -> List[str]:
    """Swap one name for another in a preference list, in place, de-duplicated."""
    out: List[str] = []
    for name in names:
        candidate = new if name == old else name
        if candidate not in out:
            out.append(candidate)
    return out


def _rename_prefs(path: Path, old: str, new: str) -> None:
    prefs = store.load(path)
    categories = prefs.categories.model_copy(update={
        "order": _rename_in(prefs.categories.order, old, new),
        "hidden": _rename_in(prefs.categories.hidden, old, new),
    })
    # The filter chips carry the category name too (`p:<name>`, design.md D9),
    # and a chip pointing at a category that no longer exists is a chip that
    # filters to nothing.
    chips = prefs.chips.model_copy(update={
        "order": _rename_in(prefs.chips.order, "p:%s" % old, "p:%s" % new),
        "hidden": _rename_in(prefs.chips.hidden, "p:%s" % old, "p:%s" % new),
    })
    widget = prefs.widget
    if widget.category == old:
        widget = widget.model_copy(update={"category": new})
    _write_if_changed(path, prefs, categories=categories.model_dump(),
                      chips=chips.model_dump(), widget=widget.model_dump())


def _drop_prefs(path: Path, name: str, move_to: Optional[str]) -> None:
    prefs = store.load(path)
    categories = prefs.categories.model_copy(update={
        "order": [c for c in prefs.categories.order if c != name],
        "hidden": [c for c in prefs.categories.hidden if c != name],
    })
    chip = "p:%s" % name
    chips = prefs.chips.model_copy(update={
        "order": [c for c in prefs.chips.order if c != chip],
        "hidden": [c for c in prefs.chips.hidden if c != chip],
    })
    widget = prefs.widget
    if widget.category == name:
        # The widget would otherwise filter to a category nothing is in and
        # show an empty home screen with no way to tell why.
        widget = widget.model_copy(update={"category": move_to})
    _write_if_changed(path, prefs, categories=categories.model_dump(),
                      chips=chips.model_dump(), widget=widget.model_dump())


def _write_if_changed(path: Path, before, **sections) -> None:
    """Skip the write when nothing moved — so a shared rename does not create a
    prefs file of pure defaults for a user who has never opened Settings."""
    current = before.model_dump()
    if all(current.get(k) == v for k, v in sections.items()):
        return
    store.update(path, **sections)


def _everyone_prefs() -> List[Path]:
    return [u.prefs_path for u in stores.users()]


async def _private_holders(name: str, exclude: Optional[User] = None) -> List[str]:
    """Users (other than `exclude`) with a non-deleted private task named `name`."""
    holders = []
    for u in stores.users():
        if exclude is not None and u.name == exclude.name:
            continue
        if await tw.export(NOT_DELETED, "project.is:%s" % name, store=u.store):
            holders.append(u.name or "default")
    return holders


def _moved(moved: int, left: int) -> Response:
    """204 carrying the counts. `X-Moved` is the contract's; `X-Left` is the
    recurring ones left behind, so the client can say "3 moved, 1 recurring
    left private" without guessing. Both are in main.py's CORS expose list —
    without that the app at capacitor://localhost cannot read either."""
    return Response(status_code=204,
                    headers={"X-Moved": str(moved), "X-Left": str(left)})


# --------------------------------------------------------------------------- #
# rename / delete
# --------------------------------------------------------------------------- #
@router.post("/rename", status_code=204)
async def rename_category(request: Request, body: CategoryRename):
    """`{"from","to"}` → 204. Moves every non-deleted task and the prefs with it."""
    old, new = body.from_, body.to
    if old == new:
        return Response(status_code=204)
    user = stores.caller(request)
    shared = stores.shared_store()
    names = stores.shared_names()

    if shared and old in names:
        # Shared -> anything: runs in the shared store. Onto a name somebody
        # holds privately would put one name on both sides of the line.
        if new not in names:
            holders = await _private_holders(new)
            if holders:
                raise conflict("%r is a private category of %s; rename stays a "
                               "rename — make %r private first"
                               % (new, ", ".join(holders), old))
        moved = await tw.bulk_modify([NOT_DELETED, "project.is:%s" % old],
                                     ["project:%s" % new], store=shared)
        stores.edit_shared_names(lambda ns: [new if n == old else n for n in ns])
        for path in _everyone_prefs():
            _rename_prefs(path, old, new)
        log.info("shared category rename %s -> %s (%d task(s))", old, new, moved)
        return Response(status_code=204)

    if shared and new in names:
        raise conflict("%r is a shared category; rename stays a rename — use "
                       "share to publish %r" % (new, old))

    moved = await tw.bulk_modify([NOT_DELETED, "project.is:%s" % old],
                                 ["project:%s" % new], store=user.store)
    _rename_prefs(user.prefs_path, old, new)
    log.info("category rename %s -> %s (%d task(s))", old, new, moved)
    return Response(status_code=204)


@router.post("/delete", status_code=204)
async def delete_category(request: Request, body: CategoryDelete):
    """`{"name","move_to"}` → 204. The tasks survive; only the category goes."""
    name = body.name
    if body.move_to == name:
        # Moving a category's tasks into itself is the one case that cannot
        # mean what it says: the endpoint would report success having removed
        # the name from the prefs while every task still carries it.
        raise invalid("move_to", "cannot be the category being deleted")
    user = stores.caller(request)
    shared = stores.shared_store()
    names = stores.shared_names()

    if shared and name in names:
        # Tasks stay in the shared store, so their new name must be a shared
        # one (or none) — anything else is a private name in the shared store.
        if body.move_to is not None and body.move_to not in names:
            raise invalid("move_to", "must be a shared category or null")
        moved = await tw.bulk_modify([NOT_DELETED, "project.is:%s" % name],
                                     ["project:%s" % (body.move_to or "")],
                                     store=shared)
        stores.edit_shared_names(lambda ns: [n for n in ns if n != name])
        for path in _everyone_prefs():
            _drop_prefs(path, name, body.move_to)
        log.info("shared category delete %s -> %s (%d task(s))", name,
                 body.move_to or "(none)", moved)
        return Response(status_code=204)

    if shared and body.move_to in names:
        # Not in the contract's text: the mirror of rename's 409. Moving
        # private tasks under a shared name in the private store would leave
        # that name on both sides of the line; share is how tasks cross it.
        raise conflict("%r is a shared category; share %r instead, or move "
                       "its tasks one by one" % (body.move_to, name))

    moved = await tw.bulk_modify([NOT_DELETED, "project.is:%s" % name],
                                 ["project:%s" % (body.move_to or "")],
                                 store=user.store)
    _drop_prefs(user.prefs_path, name, body.move_to)
    log.info("category delete %s -> %s (%d task(s))", name,
             body.move_to or "(none)", moved)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# share / unshare (round 9)
# --------------------------------------------------------------------------- #
@router.post("/share", status_code=204)
async def share_category(request: Request, body: CategoryName):
    """`{"name"}` → 204 + X-Moved. Publishes the caller's tasks in `name`."""
    shared = stores.shared_store()
    if shared is None:
        raise conflict("no users configured — there is no shared store")
    name = body.name
    if name in stores.shared_names():
        return _moved(0, 0)
    user = stores.caller(request)
    holders = await _private_holders(name, exclude=user)
    if holders:
        # Sharing would publish their tasks without asking them.
        raise conflict("%s also has a private %r; sharing it would publish "
                       "their tasks" % (", ".join(holders), name))

    rows = await tw.export(NOT_DELETED, "project.is:%s" % name, store=user.store)
    moved, left = await moves.move_all(rows, user.store, shared)
    # Listed only once the move has gone through: if it died half way, the
    # name is still private and sharing again picks up the rest — rather than
    # "already shared, nothing moved" stranding them.
    stores.edit_shared_names(lambda ns: ns + [name])
    log.info("shared %s by %s: %d moved, %d recurring left", name, user.name,
             moved, left)
    return _moved(moved, left)


@router.post("/unshare", status_code=204)
async def unshare_category(request: Request, body: CategoryName):
    """`{"name"}` → 204 + X-Moved. Every task in it goes to the CALLER."""
    shared = stores.shared_store()
    name = body.name
    if shared is None or name not in stores.shared_names():
        return _moved(0, 0)
    user = stores.caller(request)

    rows = await tw.export(NOT_DELETED, "project.is:%s" % name, store=shared)
    moved, left = await moves.move_all(rows, shared, user.store)
    stores.edit_shared_names(lambda ns: [n for n in ns if n != name])
    for path in _everyone_prefs():
        _drop_prefs(path, name, None)
    log.info("unshared %s to %s: %d moved, %d recurring left in shared", name,
             user.name, moved, left)
    return _moved(moved, left)
