"""Moving tasks between stores (docs/api.md round 9, docs/design.md D19).

A category change that crosses the private/shared line, and Share / Make
private, are all the same four steps, verified on Taskwarrior 3.4.2 in a
throwaway pair of stores before any of this was written:

    task <uuid> export        (source)
    task import -             (destination; the JSON on stdin)
    task <uuid> delete        (source; also fine on a completed task)
    task <uuid> purge         (source; the record is then gone entirely)

The uuid, annotations, tags, due, priority, `order` and `depends` all survive
the import, so the phone's row never has to be re-keyed. **Import first**: if
anything after it fails, the task exists twice rather than nowhere, and the
next uuid lookup resolves the duplicate (routers/tasks.py `_locate`, shared
copy wins). A failure after the import is a 502 that says so.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from . import taskwarrior as tw
from .errors import TaskFailed, invalid
from .stores import Store

log = logging.getLogger("taskmaster.moves")

# A sentinel for "leave the project as exported", since None means "clear it".
KEEP = object()


def is_recurring(raw: Dict[str, Any], templates: Dict[str, Dict[str, Any]]) -> bool:
    """A template, or an instance whose template is still live.

    Those cannot move: the template and its siblings would stay behind and
    keep spawning in the old store (docs/api.md round 9). An instance whose
    template was deleted by "stop repeating" is NOT refused — the API already
    reports it as a plain task (`parent: null`, serialize.task_out), so
    refusing it would be refusing a task the phone shows as not repeating.
    Verified on 3.4.2: such an orphan imports into another store and spawns
    nothing there.
    """
    if raw.get("status") == "recurring":
        return True
    parent = raw.get("parent")
    return bool(parent) and parent in templates


def _import_row(raw: Dict[str, Any], project: Any) -> Dict[str, Any]:
    row = {k: v for k, v in raw.items() if k not in ("id", "urgency")}
    if project is not KEEP:
        # The new category is written into the record being imported, so the
        # project change and the move are ONE step: there is never a moment
        # where the shared store holds a private name or the private store a
        # shared one, even if a later step fails.
        if project:
            row["project"] = project
        else:
            row.pop("project", None)
    return row


async def _remove_all(rows: List[Dict[str, Any]], src: Store, dest: Store) -> None:
    for raw in rows:
        try:
            await tw.remove(raw["uuid"], raw.get("status"), store=src)
        except TaskFailed as exc:
            raise TaskFailed(
                "task %s was copied into the %s store but could not be removed "
                "from the %s store (%s); it resolves to the shared copy at its "
                "next lookup" % (raw["uuid"], dest.name, src.name, exc.detail),
                exc.argv, exc.returncode) from exc


async def move(raw: Dict[str, Any], src: Store, dest: Store,
               project: Any = KEEP) -> None:
    """Move one task. 422 naming `project` for a recurring one."""
    if is_recurring(raw, await tw.templates(store=src)):
        raise invalid("project", "a repeating task cannot move between private "
                                 "and shared; stop it repeating first")
    await tw.import_tasks([_import_row(raw, project)], store=dest)
    await _remove_all([raw], src, dest)
    log.info("moved %s %s -> %s", raw["uuid"], src.name, dest.name)


async def move_all(rows: List[Dict[str, Any]], src: Store,
                   dest: Store) -> Tuple[int, int]:
    """Bulk move, leaving recurring ones where they are. -> (moved, left).

    One `import` for the lot, then delete+purge one at a time — each step is a
    uuid filter, never a category filter, so nothing the export did not
    return can be touched.
    """
    if not rows:
        return 0, 0
    templates = await tw.templates(store=src)
    movable = [r for r in rows if not is_recurring(r, templates)]
    left = len(rows) - len(movable)
    await tw.import_tasks([_import_row(r, KEEP) for r in movable], store=dest)
    await _remove_all(movable, src, dest)
    log.info("moved %d task(s) %s -> %s, %d recurring left", len(movable),
             src.name, dest.name, left)
    return len(movable), left
