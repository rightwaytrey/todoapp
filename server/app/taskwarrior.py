"""The only module in the server that runs `task`.

Everything else calls these coroutines. Three rules hold the whole design up:

* **argv, never a shell.** `subprocess.run([...])` with a list and no
  `shell=True`. A description is user text that may contain `;`, `$(...)`,
  quotes or newlines, and it reaches Taskwarrior after a literal `--` so the
  CLI's own filter grammar cannot claim it either (that is what keeps
  "call bob due:tomorrow" a description instead of a due date, docs/api.md
  Create).

* **One lock.** Taskwarrior 3 keeps its data in a single SQLite file and takes
  its own write lock; two overlapping `task` processes surface as a hard
  failure rather than a queue. The API is single-user and every call is ~10 ms,
  so one process-wide `threading.Lock` is both sufficient and honest. Run one
  uvicorn worker (see deploy/taskmaster-api.service) — a second worker would be
  a second lock.

* **Off the event loop.** `asyncio.to_thread` — the calls are blocking
  subprocesses, and the 30 s poll from the phone must not stall behind a write.

`rc.context=none` on every invocation, for the same reason `pa` does it: the
user's ~/.taskrc defines read-contexts (`task context work`), and if one is left
applied then an unscoped `status:pending export` silently returns only the
in-context tasks. The phone list is "every pending task, no filter"
(docs/design.md D2), so the context has to be off or the list quietly lies.

`TASKRC` / `TASKDATA` are inherited from the environment, so production runs
against the user's real ~/.taskrc **with hooks on** — the on-add / on-modify
hooks are what fire pa-pushnow, which reruns `pa retag` and republishes the
Scriptable widget feed (docs/design.md D1). The tests point both at a throwaway
directory with `hooks=off`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import threading
from typing import Any, Dict, List, Optional, Sequence

from .config import settings
from .errors import TaskFailed

log = logging.getLogger("taskmaster.task")

# Serialises every `task` invocation in this process.
_LOCK = threading.Lock()

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_NEW_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Applied to every call. `rc.verbose=nothing` keeps stdout to just the JSON on
# exports and empty on writes; `add` overrides it with `new-uuid` so we can read
# the uuid back out of "Created task <uuid>.".
BASE_RC = [
    "rc.confirmation=off",
    "rc.recurrence.confirmation=off",
    "rc.context=none",
    "rc.verbose=nothing",
]


def is_uuid(value: str) -> bool:
    """A path parameter is only ever used as a Taskwarrior *filter*.

    This matters more than it looks: `task <filter> modify project:x` with a
    filter of `status:pending` would rewrite every pending task. So nothing
    reaches an argv position unless it is a full 36-character uuid, and a
    partial uuid is refused too (Taskwarrior would happily prefix-match it onto
    the wrong task).
    """
    return bool(value) and bool(UUID_RE.match(value))


def _run(args: Sequence[str], verbose: str = "nothing",
         extra_rc: Sequence[str] = ()) -> subprocess.CompletedProcess:
    """Blocking. Holds the lock for the life of the child process."""
    rc = list(BASE_RC)
    if verbose != "nothing":
        rc[-1] = "rc.verbose=%s" % verbose
    rc += list(extra_rc)
    argv = [settings.task_bin, *rc, *args]
    with _LOCK:
        try:
            return subprocess.run(argv, capture_output=True, text=True,
                                  timeout=settings.task_timeout_s)
        except FileNotFoundError as exc:
            raise TaskFailed("cannot run %s: %s" % (settings.task_bin, exc), argv) from exc
        except subprocess.TimeoutExpired as exc:
            raise TaskFailed("task timed out after %ss" % settings.task_timeout_s,
                             argv) from exc


def _check(res: subprocess.CompletedProcess, argv: Sequence[str]) -> str:
    """Return stdout, or raise TaskFailed carrying whatever the CLI complained.

    Taskwarrior 3.4.2 writes user-facing errors to **stdout** — verified on this
    box: `task <uuid> done` on an already-completed task prints "is neither
    pending nor waiting." on stdout with an empty stderr and rc=1. So take
    stderr when there is one and fall back to stdout, or the 502 detail would be
    an empty string exactly when it is needed.
    """
    if res.returncode == 0:
        return res.stdout
    detail = (res.stderr or "").strip() or (res.stdout or "").strip()
    log.warning("task failed rc=%d: %s", res.returncode, detail)
    raise TaskFailed(detail, list(argv), res.returncode)


async def _call(args: Sequence[str], verbose: str = "nothing",
                extra_rc: Sequence[str] = ()) -> str:
    res = await asyncio.to_thread(_run, args, verbose, extra_rc)
    return _check(res, args)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
async def version() -> str:
    """-> "3.4.2". Raises TaskFailed if the binary cannot be run.

    `_version`, not `--version`: 3.4.2 rejects `rc.…` overrides alongside the
    long form (exit 1, no message) but accepts them with the internal command.
    """
    res = await asyncio.to_thread(_run, ["_version"])
    return _check(res, ["_version"]).strip()


async def export(*filters: str) -> List[Dict[str, Any]]:
    """Task dicts for a filter. `export()` with no filter is the whole DB."""
    out = (await _call([*filters, "export"])).strip()
    if not out:
        return []
    return json.loads(out)


async def get(uuid: str) -> Optional[Dict[str, Any]]:
    """One task of any status, or None.

    Note `task <unknown-uuid> export` exits **0** with an empty array rather
    than failing, so "does it exist" is a length check, not a return code.
    """
    if not is_uuid(uuid):
        return None
    rows = await export(uuid)
    return rows[0] if rows else None


async def templates() -> Dict[str, Dict[str, Any]]:
    """uuid -> every live recurring template (`status:recurring`).

    The instances on the phone are pending tasks with a `parent`; their own
    `recur`/`until` are a snapshot from when they were spawned and do not
    follow the template, and an instance whose template has been deleted keeps
    both fields forever because 3.4.2 refuses to clear them. So the templates
    are what `serialize.task_out` reads the schedule out of — see its docstring
    for the two bugs that come of not doing this.
    """
    return {t["uuid"]: t for t in await export("status:recurring")}


async def uda_order_declared() -> bool:
    """Is `uda.order.type` set in the taskrc this server is running against?

    Worth a subprocess before every `order:` write, because the failure mode is
    silent data loss: with the UDA undeclared, `task <uuid> modify order:1500`
    exits **0** and sets the task's DESCRIPTION to "order:1500" — the token is
    not an attribute Taskwarrior knows, so it falls through to the description.
    Verified on 3.4.2. deploy/install.sh adds the two lines; this is what
    notices when it has not been run.
    """
    res = await asyncio.to_thread(_run, ["_get", "rc.uda.order.type"])
    return bool(_check(res, ["_get"]).strip())


async def blocked_uuids() -> set:
    """The uuids Taskwarrior itself considers blocked.

    `+BLOCKED` is Taskwarrior's virtual tag for "depends on something that is
    still pending" — exactly the `blocked` field in docs/api.md — so we let it
    do the transitive bookkeeping instead of re-deriving it from `depends`.
    """
    return {t["uuid"] for t in await export("+BLOCKED")}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
async def add(description: str, attrs: Sequence[str] = ()) -> str:
    """`task add <attrs…> -- <description>` -> the new uuid.

    `attrs` are already-validated `project:x` / `due:…` / `+tag` tokens. The
    description goes after `--` so `due:tomorrow` inside it stays literal
    (verified on 3.4.2).
    """
    out = await _call(["add", *attrs, "--", description], verbose="new-uuid")
    m = _NEW_UUID_RE.search(out)
    if not m:
        raise TaskFailed("could not read the new uuid from %r" % out.strip(),
                         ["add"])
    return m.group(1)


async def modify(uuid: str, attrs: Sequence[str],
                 description: Optional[str] = None) -> None:
    """`task <uuid> modify <attrs…> [-- <description>]`.

    An empty value clears the attribute (`due:`, `project:`, `priority:`) and a
    `+tag` / `-tag` token adds or removes one — that is the whole of PATCH.
    """
    if not attrs and description is None:
        return
    args = [uuid, "modify", *attrs]
    if description is not None:
        args += ["--", description]
    await _call(args)


async def bulk_modify(filters: Sequence[str], attrs: Sequence[str]) -> int:
    """`task <filters…> modify <attrs…>` over many tasks. -> how many matched.

    Two things this has to get right, both verified on 3.4.2 and neither
    obvious from the command line:

    * **`rc.bulk=0`.** `rc.confirmation=off` does NOT cover the bulk prompt.
      Over three pending tasks, `task project:old modify project:new` with the
      server's usual flags asks "Modify task 1 'bulk 3'? (yes/no/all/quit)",
      reads EOF, prints "Task not modified." and exits 1 — nothing changes and
      the only clue is the exit code. `rc.bulk=0` means "never prompt".

    * **Count first, then modify.** With no matching task the command exits 1
      with empty output, which would surface as a 502 whose detail is the empty
      string. Renaming a category nobody has used yet is not an error, so the
      count is what decides whether there is anything to run.
    """
    matched = await export(*filters)
    if not matched:
        return 0
    await _call([*filters, "modify", *attrs], extra_rc=["rc.bulk=0"])
    return len(matched)


async def annotate(uuid: str, text: str) -> None:
    await _call([uuid, "annotate", "--", text])


async def done(uuid: str) -> None:
    await _call([uuid, "done"])


async def undone(uuid: str) -> None:
    """Un-complete. `modify status:pending` clears `end` too (verified 3.4.2)."""
    await _call([uuid, "modify", "status:pending"])


async def delete(uuid: str) -> None:
    """Taskwarrior keeps the record with status:deleted; it is not erased."""
    await _call([uuid, "delete"])
