"""Request bodies (pydantic v2). Responses are built by hand in serialize.py so
the JSON matches docs/api.md literally rather than approximately.

Every validator raises ValueError with a short message; main.py's
RequestValidationError handler turns that into
`422 {"error":"invalid_request","detail":"<field>: <message>"}`, which is the
"naming the field" docs/api.md asks for.

Unknown keys are **ignored**, not rejected. The client is being written in
parallel against the same document; a 422 on a stray key would be a confusing
integration failure, and the contract never promises rejection.
"""
from __future__ import annotations

import math
import re
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import RESERVED_TAGS
from .serialize import parse_due_in

PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
PRIORITIES = ("H", "M", "L")

# A Taskwarrior period: `daily`, `3days`, `2weeks`, `P1M`… The CLI is the real
# judge ("The duration value 'bogusly' is not supported." on 3.4.2, which the
# 502 hands back verbatim), so this only keeps shell-ish junk out of an argv
# slot and bounds the length.
RECUR_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")

# Drag-to-reorder sends midpoints, so the values halve each time a row is moved
# into the same gap. This bound is about keeping `order:<n>` printable and the
# JSON honest, not about how many tasks there are.
ORDER_LIMIT = 1e12


# --------------------------------------------------------------------------- #
# Shared cleaners. Each raises ValueError; the field name comes from pydantic.
# --------------------------------------------------------------------------- #
def clean_description(v):
    if not isinstance(v, str):
        raise ValueError("must be a string")
    v = v.strip()
    if not v:
        raise ValueError("must not be empty")
    return v


def clean_project(v):
    if v is None:
        return None
    if not isinstance(v, str) or not PROJECT_RE.match(v.strip()):
        raise ValueError("must match ^[A-Za-z0-9_.-]{1,40}$")
    return v.strip()


def clean_priority(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("must be one of H, M, L (or null to clear)")
    v = v.strip().upper()
    if v == "":
        return None
    if v not in PRIORITIES:
        raise ValueError("must be one of H, M, L (or null to clear)")
    return v


def clean_due(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("must be a string")
    return parse_due_in(v)


def clean_tags(v):
    """Reserved tags are dropped silently — the client never knew about them."""
    if not isinstance(v, list):
        raise ValueError("must be a list of strings")
    out: List[str] = []
    for t in v:
        if not isinstance(t, str):
            raise ValueError("entries must be strings")
        t = t.strip()
        if t in RESERVED_TAGS:
            continue
        if not TAG_RE.match(t):
            raise ValueError("%r must match ^[A-Za-z0-9_]{1,40}$" % t)
        if t not in out:
            out.append(t)
    return out


def clean_recur(v):
    """A period, passed to Taskwarrior verbatim.

    **Not lower-cased**, and that is a correction, not an oversight: 3.4.2's
    periods are case-sensitive in both directions. `P1M` (ISO-8601) is accepted
    and `p1m` is not; `weekly` is accepted and `Weekly` is not. Folding the case
    either way breaks half of them, so the string is passed through and
    Taskwarrior is left to judge it — which is what "any period Taskwarrior
    accepts" meant. An unsupported one comes back as a 502 carrying the CLI's
    own words.
    """
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("must be a Taskwarrior period like daily or 2weeks")
    v = v.strip()
    if v == "":
        return None
    if not RECUR_RE.match(v):
        raise ValueError("must be a Taskwarrior period like daily or 2weeks")
    return v


def clean_order(v):
    """A position on the list, or null to unplace it.

    `bool` is checked before `int` on purpose: in Python `True` is an `int`,
    and `{"order": true}` reaching Taskwarrior as `order:1` would silently
    place a task at the top.
    """
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("must be a number (or null to unplace it)")
    if not math.isfinite(v):
        raise ValueError("must be a finite number")
    if abs(v) > ORDER_LIMIT:
        raise ValueError("must be between -1e12 and 1e12")
    return v


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str
    project: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[str] = None
    tags: Optional[List[str]] = None

    @field_validator("description")
    @classmethod
    def v_description(cls, v):
        return clean_description(v)

    @field_validator("project")
    @classmethod
    def v_project(cls, v):
        return clean_project(v)

    @field_validator("priority")
    @classmethod
    def v_priority(cls, v):
        return clean_priority(v)

    @field_validator("due")
    @classmethod
    def v_due(cls, v):
        return clean_due(v)

    @field_validator("tags")
    @classmethod
    def v_tags(cls, v):
        return None if v is None else clean_tags(v)


class TaskPatch(BaseModel):
    """Any subset of the eight. A field that is *present and null* clears the
    attribute; a field that is absent is untouched — the router tells the two
    apart with `model_fields_set`, which is why nothing here needs a sentinel."""

    model_config = ConfigDict(extra="ignore")

    description: Optional[str] = None
    project: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[str] = None
    tags: Optional[List[str]] = None
    # The three added in round 5. `recur` and `until` are not plain attribute
    # writes — they land on the recurring *template*, or make one — so the
    # router handles them apart from `attrs` (see routers/tasks.py).
    order: Optional[Union[int, float]] = None
    recur: Optional[str] = None
    until: Optional[str] = None

    @field_validator("description")
    @classmethod
    def v_description(cls, v):
        # A description cannot be cleared: Taskwarrior has no empty-description
        # task, and `modify -- ""` is a silent no-op rather than an error.
        if v is None:
            raise ValueError("cannot be null; omit it to leave it unchanged")
        return clean_description(v)

    @field_validator("project")
    @classmethod
    def v_project(cls, v):
        return clean_project(v)

    @field_validator("priority")
    @classmethod
    def v_priority(cls, v):
        return clean_priority(v)

    @field_validator("due")
    @classmethod
    def v_due(cls, v):
        return clean_due(v)

    @field_validator("tags")
    @classmethod
    def v_tags(cls, v):
        # `tags` is a full replacement set, so null is meaningless: [] clears.
        if v is None:
            raise ValueError("cannot be null; send [] to clear all tags")
        return clean_tags(v)

    # mode="before" is load-bearing: pydantic's lax mode would coerce the
    # string "1000" (and `true`) into an int before a normal validator saw it,
    # and `order` is a number in the contract, not "something int() accepts".
    @field_validator("order", mode="before")
    @classmethod
    def v_order(cls, v):
        return clean_order(v)

    @field_validator("recur")
    @classmethod
    def v_recur(cls, v):
        return clean_recur(v)

    @field_validator("until")
    @classmethod
    def v_until(cls, v):
        return clean_due(v)


class AnnotationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str

    @field_validator("text")
    @classmethod
    def v_text(cls, v):
        return clean_description(v)


# --------------------------------------------------------------------------- #
# Categories (docs/design.md D13: a category *is* Taskwarrior's `project`)
# --------------------------------------------------------------------------- #
class CategoryRename(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # `from` is a Python keyword, so the field is `from_` and the alias is what
    # the JSON carries. `populate_by_name` is deliberately NOT set: the wire
    # name is the contract's, and accepting `from_` too would be a second
    # spelling for one thing.
    from_: str = Field(alias="from")
    to: str

    @field_validator("from_", "to")
    @classmethod
    def v_name(cls, v):
        name = clean_project(v)
        if name is None:
            raise ValueError("must match ^[A-Za-z0-9_.-]{1,40}$")
        return name


class CategoryDelete(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    # Where the orphaned tasks go. null means "lose the category", which is
    # `project:` in Taskwarrior terms — not the same as leaving them alone.
    move_to: Optional[str] = None

    @field_validator("name")
    @classmethod
    def v_name(cls, v):
        name = clean_project(v)
        if name is None:
            raise ValueError("must match ^[A-Za-z0-9_.-]{1,40}$")
        return name

    @field_validator("move_to")
    @classmethod
    def v_move_to(cls, v):
        return clean_project(v)
