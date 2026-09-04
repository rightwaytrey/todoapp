"""The preferences document (docs/api.md round 5, docs/design.md D15).

One JSON file at `settings.prefs_path`, read on every request that needs it and
replaced whole by `PUT /api/prefs`. It is small (a few hundred bytes) and this
process is the only writer, so there is no cache to invalidate and an edit made
with `$EDITOR` takes effect on the next request — which is the behaviour you
want from a settings file on a home server.

**Reading never raises.** A missing, unreadable or malformed file reads as the
defaults, for the same reason app_update.read_manifest() does: the resting state
of a fresh install is "no file", and one bad byte here must not take the task
list down with it. A `PUT` is the only thing that can complain, and it complains
in the docs/api.md envelope naming the field.

Validation is pydantic, defaults are the ones in docs/api.md, and unknown keys
are dropped rather than rejected — the client and the widget are written against
the same document and a stray key must not be an integration failure.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import PA_PROJECTS, settings

log = logging.getLogger("taskmaster.prefs")

# Same names the rest of the server uses. `clean_project` lives in schemas.py,
# but schemas.py imports nothing from here and this module must not import it
# back, so the two patterns are spelled out rather than shared through a third
# module for four lines.
PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")

SORT_MODES = ("due", "priority", "urgency", "manual")
GROUPS = ("overdue", "today", "upcoming", "none")
# What the widget's rows are grouped by (docs/api.md round 6). "due" is what
# round 5 did and stays the default: a widget that regroups itself the moment
# the server learns a new word would be a surprise on someone's home screen.
GROUP_BYS = ("due", "category")

# The two tags the filter bar offers (docs/design.md D9) — the default chip row
# is the five pa categories then those, which is what the client shipped with.
DEFAULT_CHIPS = ["p:%s" % p for p in PA_PROJECTS] + ["t:claude", "t:alert"]

# Serialises the read-modify-write in save(). One process (one uvicorn worker,
# see deploy/taskmaster-api.service), so a threading.Lock is the whole story —
# the same argument taskwarrior.py makes for its lock.
_LOCK = threading.Lock()


def _names(values: Any, pattern: re.Pattern, what: str) -> List[str]:
    """A list of validated, de-duplicated names, order preserved."""
    if not isinstance(values, list):
        raise ValueError("must be a list of strings")
    out: List[str] = []
    for v in values:
        if not isinstance(v, str):
            raise ValueError("entries must be strings")
        v = v.strip()
        if not pattern.match(v):
            raise ValueError("%r must match %s" % (v, what))
        if v not in out:
            out.append(v)
    return out


def _chip_ids(values: Any) -> List[str]:
    """`p:<category>` / `t:<tag>` — the shape is checked, the name is not.

    A chip naming a category nobody uses any more is *ignored* by the client,
    not an error: the row is a preference that outlives the tasks it points at,
    and rejecting it here would make deleting a category fail a later PUT.
    """
    if not isinstance(values, list):
        raise ValueError("must be a list of strings")
    out: List[str] = []
    for v in values:
        if not isinstance(v, str):
            raise ValueError("entries must be strings")
        v = v.strip()
        if v.startswith("p:") and PROJECT_RE.match(v[2:]):
            ok = True
        elif v.startswith("t:") and TAG_RE.match(v[2:]):
            ok = True
        else:
            ok = False
        if not ok:
            raise ValueError("%r must be p:<category> or t:<tag>" % v)
        if v not in out:
            out.append(v)
    return out


class CategoryPrefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order: List[str] = Field(default_factory=lambda: list(PA_PROJECTS))
    hidden: List[str] = Field(default_factory=list)

    @field_validator("order", "hidden")
    @classmethod
    def v_names(cls, v):
        return _names(v, PROJECT_RE, "^[A-Za-z0-9_.-]{1,40}$")


class ChipPrefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order: List[str] = Field(default_factory=lambda: list(DEFAULT_CHIPS))
    hidden: List[str] = Field(default_factory=list)

    @field_validator("order", "hidden")
    @classmethod
    def v_chips(cls, v):
        return _chip_ids(v)


class SortPrefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str = "due"

    @field_validator("mode")
    @classmethod
    def v_mode(cls, v):
        if not isinstance(v, str) or v.strip() not in SORT_MODES:
            raise ValueError("must be one of %s" % ", ".join(SORT_MODES))
        return v.strip()


class RowCaps(BaseModel):
    """How many rows each widget family draws. The server caps its own feed at
    `large` and echoes all three so the widget can truncate without a build."""

    model_config = ConfigDict(extra="ignore")

    small: int = 3
    medium: int = 5
    large: int = 12

    # mode="before" throughout this module wherever the type is int or bool:
    # pydantic's lax mode turns "7" into 7 and "yes" into True, and a
    # preferences document that silently reinterprets what the client sent is
    # exactly the kind of thing nobody finds until the widget draws it.
    @field_validator("small", "medium", "large", mode="before")
    @classmethod
    def v_cap(cls, v):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer")
        if not 0 <= v <= 50:
            raise ValueError("must be between 0 and 50")
        return v


class WidgetPrefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    groups: List[str] = Field(default_factory=lambda: ["overdue", "today"])
    upcoming_days: int = 7
    category: Optional[str] = None
    rows: RowCaps = Field(default_factory=RowCaps)
    show_category: bool = False
    group_by: str = "due"

    @field_validator("groups")
    @classmethod
    def v_groups(cls, v):
        got = _names(v, re.compile(r"^[a-z]+$"), "a group name")
        for g in got:
            if g not in GROUPS:
                raise ValueError("%r must be one of %s" % (g, ", ".join(GROUPS)))
        return got

    @field_validator("upcoming_days", mode="before")
    @classmethod
    def v_days(cls, v):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer")
        if not 0 <= v <= 365:
            raise ValueError("must be between 0 and 365")
        return v

    @field_validator("category")
    @classmethod
    def v_category(cls, v):
        if v is None:
            return None
        if not isinstance(v, str) or not PROJECT_RE.match(v.strip()):
            raise ValueError("must match ^[A-Za-z0-9_.-]{1,40}$ (or null for all)")
        return v.strip()

    @field_validator("show_category", mode="before")
    @classmethod
    def v_show(cls, v):
        if not isinstance(v, bool):
            raise ValueError("must be true or false")
        return v

    @field_validator("group_by")
    @classmethod
    def v_group_by(cls, v):
        if not isinstance(v, str) or v.strip() not in GROUP_BYS:
            raise ValueError("must be one of %s" % ", ".join(GROUP_BYS))
        return v.strip()


class Prefs(BaseModel):
    """The whole document. Every section defaults, so `Prefs()` is what a box
    with no file answers with and what a `PUT {}` stores."""

    model_config = ConfigDict(extra="ignore")

    categories: CategoryPrefs = Field(default_factory=CategoryPrefs)
    chips: ChipPrefs = Field(default_factory=ChipPrefs)
    sort: SortPrefs = Field(default_factory=SortPrefs)
    widget: WidgetPrefs = Field(default_factory=WidgetPrefs)


# --------------------------------------------------------------------------- #
# the file
# --------------------------------------------------------------------------- #
def load() -> Prefs:
    """The stored document, or the defaults. Never raises.

    A file the user hand-edited into nonsense is logged once per read and then
    ignored, which is noisier than caching it and quieter than a 500 on every
    poll of the task list.
    """
    try:
        raw = json.loads(settings.prefs_path.read_text())
    except FileNotFoundError:
        return Prefs()
    except (OSError, ValueError) as exc:
        log.warning("prefs at %s are unreadable (%s) — using defaults",
                    settings.prefs_path, exc)
        return Prefs()
    if not isinstance(raw, dict):
        log.warning("prefs at %s are not a JSON object — using defaults",
                    settings.prefs_path)
        return Prefs()
    try:
        return Prefs.model_validate(raw)
    except Exception as exc:                                     # noqa: BLE001
        # Validation failing on a *stored* document is not the caller's
        # problem: it means the file was written by hand or by an older
        # version. Fall back rather than break every request that reads it.
        log.warning("prefs at %s did not validate (%s) — using defaults",
                    settings.prefs_path, exc)
        return Prefs()


def save(prefs: Prefs) -> Prefs:
    """Write the document atomically. Returns what was written.

    Same shape as scripts/publish_bundle.py's manifest write: a sibling `.tmp`
    and one `os.replace`, so a reader either sees the old document or the new
    one and never a half-written file. The rename is atomic only within a
    filesystem, which is why the temp file is a sibling and not in /tmp.
    """
    with _LOCK:
        return _write(prefs)


def _write(prefs: Prefs) -> Prefs:
    """The write itself. Callers hold _LOCK."""
    body = json.dumps(prefs.model_dump(), indent=2, sort_keys=True) + "\n"
    path = settings.prefs_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)
    return prefs


def update(**sections: Any) -> Prefs:
    """Read-modify-write one or more top-level sections, under the lock.

    Used by the category endpoints, which rewrite `categories` and `chips`
    after a rename and must not clobber `sort` or `widget` written between
    their own read and write.
    """
    with _LOCK:
        current = load().model_dump()
        current.update(sections)
        return _write(Prefs.model_validate(current))


def as_json(prefs: Prefs) -> Dict[str, Any]:
    """The document as the API returns it — defaults already filled in."""
    return prefs.model_dump()
