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

import re
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from .config import RESERVED_TAGS
from .serialize import parse_due_in

PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
PRIORITIES = ("H", "M", "L")


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
    """Any subset of the five. A field that is *present and null* clears the
    attribute; a field that is absent is untouched — the router tells the two
    apart with `model_fields_set`, which is why nothing here needs a sentinel."""

    model_config = ConfigDict(extra="ignore")

    description: Optional[str] = None
    project: Optional[str] = None
    priority: Optional[str] = None
    due: Optional[str] = None
    tags: Optional[List[str]] = None

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


class AnnotationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str

    @field_validator("text")
    @classmethod
    def v_text(cls, v):
        return clean_description(v)
