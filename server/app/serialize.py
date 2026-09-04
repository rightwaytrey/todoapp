"""Taskwarrior's export dict -> the Task object in docs/api.md, and dates back.

Kept out of taskwarrior.py so that module stays "the one place that runs the
CLI" and nothing else. The JSON here is hand-built rather than modelled with
pydantic, for the same reason carpool/server/app/serializers.py is: the shape
in docs/api.md is the contract, and building it literally makes a drift
visible in one screen.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import RESERVED_TAGS, settings

# The two shapes docs/api.md allows for `due`, and the rule that picks between
# them: a due at local midnight is date-only (digest, never pings); a due with a
# clock time pings through `pa remind` (docs/design.md D3, pa_lib.is_date_only).
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")

_STAMP = "%Y%m%dT%H%M%SZ"


def parse_stamp(stamp: Optional[str]) -> Optional[datetime]:
    """Taskwarrior's UTC export stamp 'YYYYMMDDTHHMMSSZ' -> aware local datetime."""
    if not stamp:
        return None
    try:
        naive = datetime.strptime(stamp, _STAMP)
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone.utc).astimezone(settings.tz)


def iso(stamp: Optional[str]) -> Optional[str]:
    """Export stamp -> local ISO-8601 with offset, e.g. 2026-09-03T00:52:06-05:00."""
    dt = parse_stamp(stamp)
    return dt.isoformat() if dt else None


def due_out(stamp: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """-> (due, due_at). `due` is the local wall-clock string in one of the two
    shapes; `due_at` is the same instant with its offset spelled out."""
    dt = parse_stamp(stamp)
    if dt is None:
        return None, None
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d"), dt.isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M"), dt.isoformat()


def parse_due_in(value: str) -> str:
    """Validate a client `due` and return it unchanged for `due:<value>`.

    Taskwarrior reads both shapes as *local* time already (verified on 3.4.2:
    `due:2026-09-05T14:30` exports as 20260905T193000Z in CDT), so there is no
    conversion to do here — only a check that we are not about to hand the CLI
    something it would reinterpret as a relative date like `tomorrow`.

    Raises ValueError with the message docs/api.md wants in `detail`.
    """
    v = (value or "").strip()
    if DATE_RE.match(v):
        fmt = "%Y-%m-%d"
    elif DATETIME_RE.match(v):
        fmt = "%Y-%m-%dT%H:%M"
    else:
        raise ValueError("expected YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    try:
        datetime.strptime(v, fmt)
    except ValueError:
        raise ValueError("not a real date/time") from None
    return v


def user_tags(raw: Dict[str, Any]) -> List[str]:
    """The task's tags with the `pa retag` maintenance tags removed.

    `today` / `overdue` / `due` are painted on by a timer for the old Taskchamp
    filter (docs/design.md). Showing them would put three meaningless chips on
    most rows, and — worse — letting a PATCH send the list back would let the
    phone delete state the desktop owns.
    """
    return [t for t in (raw.get("tags") or []) if t not in RESERVED_TAGS]


def task_out(raw: Dict[str, Any], blocked: Optional[set] = None) -> Dict[str, Any]:
    """One export dict -> one Task (docs/api.md "The Task object")."""
    due, due_at = due_out(raw.get("due"))

    depends = raw.get("depends") or []
    if isinstance(depends, str):                 # Taskwarrior 2.x shape, just in case
        depends = [d for d in depends.split(",") if d]

    annotations = [
        {"entry": iso(a.get("entry")), "text": a.get("description", "")}
        for a in (raw.get("annotations") or [])
    ]

    return {
        # `id`, Taskwarrior's renumbering integer, is deliberately absent: it
        # changes whenever anything completes, so a phone holding one would act
        # on the wrong task. Everything is addressed by uuid (docs/api.md).
        "uuid": raw.get("uuid"),
        "description": raw.get("description", ""),
        "status": raw.get("status"),
        "project": raw.get("project") or None,
        "priority": raw.get("priority") or None,
        "due": due,
        "due_at": due_at,
        "tags": user_tags(raw),
        "annotations": annotations,
        "recur": raw.get("recur") or None,
        "parent": raw.get("parent") or None,
        "depends": list(depends),
        "blocked": bool(blocked and raw.get("uuid") in blocked),
        "urgency": float(raw.get("urgency") or 0.0),
        "entry": iso(raw.get("entry")),
        "modified": iso(raw.get("modified")),
        "end": iso(raw.get("end")),
    }


def now_iso() -> str:
    """The server's own clock, local, with offset — `time` / `now` in the API."""
    return datetime.now(settings.tz).isoformat()
