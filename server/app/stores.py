"""Who is calling, and which Taskwarrior stores are theirs (docs/api.md round 9).

docs/design.md D19: three stores, not one with an owner field. The default
user's private store is the ambient `TASKRC`/`TASKDATA` the server has always
run against (`~/.task`, the hooks, `pa`); every other user gets
`$TASKMASTER_STORES/<name>/`, and the shared categories live in
`$TASKMASTER_STORES/shared/`. Privacy is then a property of the filesystem:
nothing in `pa` reads the other two, so nothing in `pa` needs an owner filter.

With `TASKMASTER_USERS` unset there is one synthetic default user whose store
is the ambient one and there is **no shared store** — `shared_store()` is None
and every caller of it falls through to exactly the pre-round-9 code path.

**Hooks.** With `TASKDATA` set, 3.4.2 looks for hooks under that data
directory (`rc.debug.hooks=1` prints "Hook directory not readable:
<TASKDATA>/hooks"; verified 2026-10-08), so the other stores have no hooks to
run. `hooks=off` in the taskrc this module writes, and `rc.hooks=off` on
every argv for a non-ambient store (taskwarrior._run), are belt and braces
on top of that: a stray `hooks/` dropped into a store directory still fires
nothing.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import SHARED, settings

log = logging.getLogger("taskmaster.stores")

# The same lines tests/conftest.py writes for the throwaway store, for the same
# reasons: hooks off (above), no prompts, and the `order` UDA declared — with
# it undeclared `modify order:1500` rewrites the DESCRIPTION (verified 3.4.2,
# routers/tasks.py).
TASKRC_TEMPLATE = (
    "# Written by the TaskMaster server (server/app/stores.py,\n"
    "# docs/api.md round 9). Not ~/.taskrc: no hooks, no pa.\n"
    "data.location=%s\n"
    "hooks=off\n"
    "confirmation=off\n"
    "recurrence=on\n"
    "uda.order.type=numeric\n"
    "uda.order.label=Order\n"
)


@dataclass(frozen=True)
class Store:
    """One Taskwarrior data directory. `root is None` = the ambient one."""

    name: str
    shared: bool
    root: Optional[Path]

    @property
    def taskrc(self) -> Optional[Path]:
        return self.root / "taskrc" if self.root else None

    @property
    def data(self) -> Optional[Path]:
        return self.root / "data" if self.root else None

    def env(self) -> Optional[Dict[str, str]]:
        """The child's environment, or None to inherit ours unchanged.

        Exactly what the tests do for the throwaway store: TASKRC/TASKDATA
        exported to the child. TASKDATA wins over `data.location`, so even a
        taskrc edited to point at ~/.task still writes here.
        """
        if self.root is None:
            return None
        env = dict(os.environ)
        env["TASKRC"] = str(self.taskrc)
        env["TASKDATA"] = str(self.data)
        return env


@dataclass(frozen=True)
class User:
    name: Optional[str]          # None in single-user mode (meta.user: null)
    default: bool
    addrs: tuple
    store: Store
    prefs_path: Path


AMBIENT = Store(name="default", shared=False, root=None)


def multi_user() -> bool:
    return bool(settings.users)


def users() -> List[User]:
    """Every configured user, default first. Single-user: one synthetic one.

    Rebuilt from `settings` on each call (a handful of objects), so a
    `reload_settings()` in the tests is all it takes to switch modes.
    """
    if not settings.users:
        return [User(None, True, (), AMBIENT, settings.prefs_path)]
    out = []
    for spec in settings.users:
        if spec.default:
            out.append(User(spec.name, True, spec.addrs, AMBIENT,
                            settings.prefs_path))
        else:
            root = settings.stores_dir / spec.name
            out.append(User(spec.name, False, spec.addrs,
                            Store(spec.name, False, root),
                            root / "prefs.json"))
    return out


def default_user() -> User:
    return users()[0]


def shared_store() -> Optional[Store]:
    if not settings.users:
        return None
    return Store(SHARED, True, settings.stores_dir / SHARED)


def resolve(host: Optional[str]) -> User:
    """The user a peer address belongs to. Mapped => that user; otherwise
    (loopback, unmapped) => default.

    A mapped address wins even when it is a loopback one: mapping 127.0.0.2
    is an explicit act, and it is what lets the whole two-user path be driven
    end to end on the box with `curl --interface`. Never raises and never
    refuses: the allowlist in middleware.py has already decided whether this
    address may talk at all; this only decides whose list it sees. An
    unmapped tailnet address being the default user is today's behaviour,
    which is why the map must be set before her phone installs.
    """
    everyone = users()
    if host and len(everyone) > 1:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return everyone[0]
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        for u in everyone:
            if ip in u.addrs:
                return u
    return everyone[0]


def caller(request) -> User:
    """The user AccessControl resolved for this request (scope["state"])."""
    state = request.scope.get("state") or {}
    user = state.get("tm_user")
    if user is not None:
        return user
    client = request.client
    return resolve(client.host if client else None)


# --------------------------------------------------------------------------- #
# On disk
# --------------------------------------------------------------------------- #
def ensure_all() -> None:
    """Write any missing store directory and taskrc. Idempotent.

    Called at startup (main.py). An existing taskrc is never rewritten — it is
    the user's to edit — but `rc.hooks=off` on every argv covers the one line
    that must not be edited away.
    """
    stores = [u.store for u in users() if u.store.root] + \
        [s for s in [shared_store()] if s]
    for store in stores:
        store.data.mkdir(parents=True, exist_ok=True)
        if not store.taskrc.exists():
            store.taskrc.write_text(TASKRC_TEMPLATE % store.data)
            log.info("wrote %s", store.taskrc)


# The shared category list. One lock for its read-modify-writes, like prefs.py.
_LOCK = threading.Lock()


def _categories_path() -> Optional[Path]:
    s = shared_store()
    return s.root / "categories.json" if s else None


def shared_names() -> List[str]:
    """The shared categories, `[]` in single-user mode. Never raises.

    A file that will not parse reads as no shared categories, and says so in
    the journal: one bad byte must not take the task list down (the same rule
    as prefs.load). The cost is that a new task in such a category goes to
    its creator's private store until the file is fixed — private by default,
    which is the safe direction to fail.
    """
    path = _categories_path()
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("%s is unreadable (%s) — no shared categories", path, exc)
        return []
    names = raw.get("categories") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        log.warning("%s has no categories list — no shared categories", path)
        return []
    out: List[str] = []
    for n in names:
        if isinstance(n, str) and n and n not in out:
            out.append(n)
    return out


def save_shared_names(names: List[str]) -> None:
    """Atomic: a sibling .tmp and one os.replace, as prefs._write does."""
    path = _categories_path()
    assert path is not None, "no shared store in single-user mode"
    body = json.dumps({"categories": names}, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)


def edit_shared_names(fn) -> List[str]:
    """Read-modify-write the list under the lock. `fn(list) -> list`."""
    with _LOCK:
        names = fn(shared_names())
        deduped: List[str] = []
        for n in names:
            if n not in deduped:
                deduped.append(n)
        save_shared_names(deduped)
        return deduped
