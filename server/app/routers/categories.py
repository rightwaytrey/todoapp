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
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Response

from .. import prefs as store
from .. import taskwarrior as tw
from ..errors import invalid
from ..schemas import CategoryDelete, CategoryRename

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


@router.post("/rename", status_code=204)
async def rename_category(body: CategoryRename):
    """`{"from","to"}` → 204. Moves every non-deleted task and the prefs with it."""
    old, new = body.from_, body.to
    if old == new:
        return Response(status_code=204)

    moved = await tw.bulk_modify([NOT_DELETED, "project.is:%s" % old],
                                ["project:%s" % new])

    prefs = store.load()
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
    store.update(categories=categories.model_dump(), chips=chips.model_dump(),
                 widget=widget.model_dump())

    log.info("category rename %s -> %s (%d task(s))", old, new, moved)
    return Response(status_code=204)


@router.post("/delete", status_code=204)
async def delete_category(body: CategoryDelete):
    """`{"name","move_to"}` → 204. The tasks survive; only the category goes."""
    name = body.name
    if body.move_to == name:
        # Moving a category's tasks into itself is the one case that cannot
        # mean what it says: the endpoint would report success having removed
        # the name from the prefs while every task still carries it.
        raise invalid("move_to", "cannot be the category being deleted")

    moved = await tw.bulk_modify([NOT_DELETED, "project.is:%s" % name],
                                ["project:%s" % (body.move_to or "")])

    prefs = store.load()
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
        widget = widget.model_copy(update={"category": body.move_to})
    store.update(categories=categories.model_dump(), chips=chips.model_dump(),
                 widget=widget.model_dump())

    log.info("category delete %s -> %s (%d task(s))", name,
             body.move_to or "(none)", moved)
    return Response(status_code=204)
