"""Taskwarrior's export dict -> the Task object in docs/api.md, and dates back.

Kept out of taskwarrior.py so that module stays "the one place that runs the
CLI" and nothing else. The JSON here is hand-built rather than modelled with
pydantic, for the same reason carpool/server/app/serializers.py is: the shape
in docs/api.md is the contract, and building it literally makes a drift
visible in one screen.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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


# --------------------------------------------------------------------------- #
# The canonical order (docs/api.md "Canonical order", docs/design.md D8/D14)
# --------------------------------------------------------------------------- #
# THREE implementations of this key exist and they must agree byte for byte:
# `orderKey()`/`groupOf()` in www/index.html, `sortKey`/`rowsFrom` in
# ios/App/TaskMasterWidget/TaskMasterWidget.swift, and this. Since D14 the
# server is the one that *decides* — /api/tasks and /api/widget both come out
# sorted and classified — and the other two are kept only so an optimistic row
# the phone has not sent yet lands in the right place.
#
# Everything below compares LOCAL "YYYY-MM-DD" / "HH:MM" strings and never
# builds an instant out of a due. That is not a style choice: the client's
# comment records what happens when you use UTC instead — after 19:00 CDT
# `toISOString().slice(0,10)` is already tomorrow, so every task due today
# jumps into Overdue every evening. Lexicographic order on a zero-padded date
# *is* chronological order, so plain `<` is right and cheaper.

PRIO_RANK = {"H": "0", "M": "1", "L": "2"}

# Group order on the screen: Overdue, Today, Upcoming, No date (design.md D2).
GROUP_RANK = {"overdue": 0, "today": 1, "upcoming": 2, "none": 3}


def local_now() -> datetime:
    """The server's wall clock, in the zone the contract pins (America/Chicago).

    A parameter on everything below rather than a call per task: one list
    response must classify all of its rows against ONE instant, or a task could
    be "today" and the next one "overdue" because the minute rolled over
    between them.
    """
    return datetime.now(settings.tz)


def due_parts(due: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """"2026-09-05T14:30" -> ("2026-09-05", "14:30"); date-only -> time None."""
    if not due:
        return None
    s = str(due)
    if "T" not in s:
        return s[:10], None
    i = s.index("T")
    return s[:i], s[i + 1:i + 6]


def group_of(due: Optional[str], now: Optional[datetime] = None) -> str:
    """"overdue" | "today" | "upcoming" | "none" — the client's `groupOf()`.

    Overdue is "the date is before today" OR "it is today and the clock time
    has already gone past". A DATE-ONLY due today is *not* overdue until
    tomorrow, because date-only means "some time today" (design.md D3) — that
    asymmetry is the whole reason the two due shapes exist, so it has to
    survive the move to the server.
    """
    p = due_parts(due)
    if p is None:
        return "none"
    now = now or local_now()
    date, time = p
    today = now.strftime("%Y-%m-%d")
    if date < today:
        return "overdue"
    if date > today:
        return "upcoming"
    # `<=`, not `<`: a task due at 09:00 is overdue at 09:00, the same minute
    # `pa remind` pings it. The client uses the same comparison.
    return "overdue" if (time and time <= now.strftime("%H:%M")) else "today"


def order_key(task: Dict[str, Any]) -> str:
    """The canonical within-group key (docs/api.md "Canonical order", 2).

        <YYYY-MM-DD>  the due's date part; "" when there is no due
        "T"
        <HH:MM>       "99:99" for a date-only due, so "some time that day"
                      sorts after every clocked due of the same day
        <prio>        "0" H, "1" M, "2" L, "3" none
        <uuid>        so the order is TOTAL — two tasks alike in every field
                      above still have one fixed order, and a refresh cannot
                      swap them under the user's thumb (which would also break
                      the ETag, since it is a hash of the body)

    Every field but the last is fixed width, so one string comparison is the
    whole order. Takes a serialised Task, not a raw export dict, because `due`
    is already the local wall-clock string by then.
    """
    p = due_parts(task.get("due"))
    date, time = p if p else ("", None)
    return "%sT%s%s%s" % (date, time or "99:99",
                          PRIO_RANK.get(task.get("priority"), "3"),
                          task.get("uuid") or "")


def _no_date_lead(task: Dict[str, Any]) -> float:
    """Urgency, descending, but only for the no-date group.

    docs/api.md "Canonical order" 3: the no-date group has no date to lead
    with, so urgency leads and the key breaks the ties. Returning a constant
    for every other group keeps this a single expression inside the sort key
    instead of a branch at the call site.
    """
    if task.get("group") != "none":
        return 0.0
    return -float(task.get("urgency") or 0.0)


def sort_key(task: Dict[str, Any], mode: str = "due"):
    """The display key for one already-serialised Task, per `prefs.sort.mode`.

    The group always leads — the screen is grouped (design.md D2) and no sort
    mode reorders the headings. Inside a group:

      due       the canonical key (urgency leads in the no-date group)
      priority  H > M > L > none, then the canonical key
      urgency   Taskwarrior's urgency descending, then uuid
      manual    tasks with an `order` UDA first, ascending, then the rest by
                the canonical key. `order` is ignored by every other mode, so
                a dragged list keeps its arrangement when you switch to due
                and back.

    Every tuple position holds one type for a given mode, which is what lets
    these be compared directly.
    """
    group = GROUP_RANK.get(task.get("group"), 3)
    if mode == "urgency":
        return (group, -float(task.get("urgency") or 0.0), task.get("uuid") or "")
    if mode == "priority":
        return (group, PRIO_RANK.get(task.get("priority"), "3"), order_key(task))
    if mode == "manual":
        placed = task.get("order")
        # Position 1 splits placed from unplaced, so position 2 is only ever
        # compared within one of the two halves.
        return (group, 0 if placed is not None else 1,
                float(placed) if placed is not None else 0.0,
                _no_date_lead(task), order_key(task))
    return (group, _no_date_lead(task), order_key(task))


def display_sort(tasks: List[Dict[str, Any]],
                 mode: str = "due") -> List[Dict[str, Any]]:
    """Sorted copy, in the order both clients render top to bottom."""
    return sorted(tasks, key=lambda t: sort_key(t, mode))


# --------------------------------------------------------------------------- #
# Row labels (the widget feed; the client's dueLabel())
# --------------------------------------------------------------------------- #
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]      # date.weekday()


def fmt_clock(hhmm: str) -> str:
    """"14:30" -> "2:30 pm". No %-I / %-p: those are glibc extensions and the
    client builds this string by hand, so this does too and they agree."""
    hour = int(hhmm[:2])
    return "%d:%s %s" % ((hour % 12) or 12, hhmm[3:5], "am" if hour < 12 else "pm")


def fmt_day(date: str, with_dow: bool, now: datetime) -> str:
    """"2026-09-04" -> "Thu Sep 4" (or "Sep 4"), with the year once it differs."""
    d = datetime.strptime(date, "%Y-%m-%d").date()
    base = "%s%s %d" % ("%s " % DAYS[d.weekday()] if with_dow else "",
                        MONTHS[d.month - 1], d.day)
    return base if d.year == now.year else "%s, %d" % (base, d.year)


def due_label(due: Optional[str], group: str, now: Optional[datetime] = None) -> str:
    """The one-line due label the widget draws, per docs/api.md "Widget feed".

    Overdue and today collapse to a word because the widget's whole subject is
    "what is due now" and a date would say nothing — except a clocked task due
    later today, where the time is the point. Anything further out gets the
    client's `dueLabel()` string: the weekday is carried on a date-only due and
    dropped once there is a clock time, or the row runs to two lines.
    """
    p = due_parts(due)
    if p is None:
        return ""
    if group == "overdue":
        return "overdue"
    date, time = p
    if group == "today":
        return fmt_clock(time) if time else "today"
    now = now or local_now()
    tomorrow = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    if date == tomorrow:
        head = "Tomorrow"
    else:
        head = fmt_day(date, not time, now)
    return "%s · %s" % (head, fmt_clock(time)) if time else head


def one_line(text: str) -> str:
    """A description flattened for a widget row — a Task's may contain newlines."""
    return " ".join((text or "").split())


def order_in(value: Any) -> str:
    """A client `order` -> the `order:<n>` token, without exponent notation.

    Taskwarrior's numeric UDA parser reads plain decimals; `%g` would turn a
    large integer into `1.23457e+06` and lose the value. An integral float is
    written as an integer so a round trip through the client's JSON does not
    slowly grow ".0" onto every order.
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def order_out(raw: Dict[str, Any]) -> Optional[float]:
    """The `order` UDA off an export dict, or None.

    Taskwarrior exports a declared numeric UDA as a JSON number — and an
    *undeclared* one as a string, which is the tell that ~/.taskrc is missing
    the two `uda.order.*` lines (deploy/install.sh adds them). Take the number
    either way rather than leaking a string into a field the contract types as
    `number|null`.
    """
    value = raw.get("order")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Normalised, because Taskwarrior is not consistent about it: the same
    # `modify order:1500` exports `1500` on one database and `1500.0` on
    # another. The client sends 1500 and should get 1500 back; a midpoint from
    # a drag (1250.5) stays a float.
    return int(number) if number.is_integer() else number


def task_out(raw: Dict[str, Any], blocked: Optional[set] = None,
             templates: Optional[Dict[str, Dict[str, Any]]] = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """One export dict -> one Task (docs/api.md "The Task object").

    `templates` maps uuid -> the live `status:recurring` template, and it is
    what makes `recur`/`until` mean what the client thinks they mean. Two
    things go wrong without it, both verified on 3.4.2:

    * An instance's own `recur`/`until` are a **snapshot taken when it was
      spawned**. `task <template> modify recur:weekly until:2026-12-01` leaves
      the pending instance saying `recur:daily, until:none`, so reading them
      off the instance shows the user the schedule they just replaced.
    * "Stop repeating" deletes the template, and Taskwarrior refuses to remove
      `recur` from the surviving instance ("You cannot remove the recurrence
      from a recurring task"). The row keeps `recur:daily` and a `parent`
      pointing at a deleted task forever, even though nothing will ever spawn
      again. An instance whose template is gone is therefore reported as the
      plain task it now behaves like — see routers/tasks.py and the api.md
      note under Recurrence.
    """
    due, due_at = due_out(raw.get("due"))

    depends = raw.get("depends") or []
    if isinstance(depends, str):                 # Taskwarrior 2.x shape, just in case
        depends = [d for d in depends.split(",") if d]

    annotations = [
        {"entry": iso(a.get("entry")), "text": a.get("description", "")}
        for a in (raw.get("annotations") or [])
    ]

    # Recurrence, read off the template rather than the instance (docstring).
    parent = raw.get("parent") or None
    recur = raw.get("recur") or None
    until, _ = due_out(raw.get("until"))
    if parent is not None and templates is not None:
        template = templates.get(parent)
        if template is None:
            parent, recur, until = None, None, None      # it stopped repeating
        else:
            recur = template.get("recur") or None
            until = due_out(template.get("until"))[0]

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
        "recur": recur,
        "until": until,
        "parent": parent,
        "depends": list(depends),
        "blocked": bool(blocked and raw.get("uuid") in blocked),
        "urgency": float(raw.get("urgency") or 0.0),
        # Where this row belongs on the screen and where the user dragged it —
        # decided here so the phone and the widget cannot disagree (D14).
        "group": group_of(due, now),
        "order": order_out(raw),
        "entry": iso(raw.get("entry")),
        "modified": iso(raw.get("modified")),
        "end": iso(raw.get("end")),
    }


def now_iso() -> str:
    """The server's own clock, local, with offset — `time` / `now` in the API."""
    return datetime.now(settings.tz).isoformat()
