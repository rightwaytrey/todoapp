"""The canonical order, now that the server is the one that decides it.

Two halves. The first is pure: `group_of` and `order_key` are string functions
with a clock passed in, so they are tested against tables rather than through
HTTP — a failure names the case instead of naming "the list came back wrong".
The second half drives the real API with the real `task` binary and checks that
what comes out of `GET /api/tasks` is in the order the phone will render.

The 14-case table is the one www/index.html was checked against, copied
verbatim from the client agent's scratchpad so the two implementations are
tested against the same fixtures and not merely against the same prose.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.serialize import display_sort, group_of, order_key, sort_key

from .conftest import listing, make, task

CHI = ZoneInfo("America/Chicago")


def at(text: str) -> datetime:
    """A frozen local instant, so "is it overdue yet" has one answer."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CHI)


def day(offset: int) -> str:
    """A local "YYYY-MM-DD" relative to today, for the end-to-end half."""
    return (datetime.now(CHI) + timedelta(days=offset)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# group_of — the client's groupOf(), evaluated in the server's zone
# --------------------------------------------------------------------------- #
NOW = at("2026-09-05 09:00")

GROUP_CASES = [
    # (due, expected group, why)
    (None, "none", "no due at all"),
    ("2026-09-04", "overdue", "date-only, yesterday"),
    ("2026-09-04T23:59", "overdue", "clocked, yesterday, late"),
    ("2026-09-05", "today", "DATE-ONLY today is today all day, never overdue"),
    ("2026-09-05T00:00", "overdue", "clocked midnight today, long past"),
    ("2026-09-05T08:59", "overdue", "clocked today, a minute ago"),
    ("2026-09-05T09:00", "overdue", "clocked today, THIS minute — <= not <"),
    ("2026-09-05T09:01", "today", "clocked today, a minute away"),
    ("2026-09-05T23:59", "today", "clocked today, tonight"),
    ("2026-09-06", "upcoming", "date-only, tomorrow"),
    ("2026-09-06T00:01", "upcoming", "clocked, just after midnight tomorrow"),
    ("2026-12-31", "upcoming", "date-only, months out"),
    ("2025-01-01", "overdue", "date-only, a year ago"),
    ("2027-01-01T09:00", "upcoming", "clocked, next year"),
]


def test_group_of_table():
    for due, expected, why in GROUP_CASES:
        assert group_of(due, NOW) == expected, "%r (%s)" % (due, why)


def test_group_of_uses_the_servers_clock_not_utc():
    """19:52 in Chicago is already tomorrow in UTC.

    This is the bug the client's comment records: build "today" out of a UTC
    instant and every evening after 19:00 CDT every task due today jumps into
    Overdue. The server has to make the same mistake impossible.
    """
    evening = at("2026-09-05 19:52")
    assert group_of("2026-09-05", evening) == "today"
    assert group_of("2026-09-06", evening) == "upcoming"


# --------------------------------------------------------------------------- #
# order_key — the 14 cases www/index.html was checked against
# --------------------------------------------------------------------------- #
A = "aaaaaaaa-0000-4000-8000-0000000000%02d"
B = "bbbbbbbb-0000-4000-8000-0000000000%02d"

CASES = {
    1:  {"uuid": A % 1,  "due": "2026-09-05T09:00", "priority": "H", "urgency": 10.0},
    2:  {"uuid": A % 2,  "due": "2026-09-05T09:00", "priority": "M", "urgency": 10.0},
    3:  {"uuid": B % 3,  "due": "2026-09-05T09:00", "priority": "M", "urgency": 99.0},
    4:  {"uuid": A % 4,  "due": "2026-09-05T08:30", "priority": None, "urgency": 1.0},
    5:  {"uuid": A % 5,  "due": "2026-09-05", "priority": "H", "urgency": 5.0},
    6:  {"uuid": A % 6,  "due": "2026-09-05", "priority": "L", "urgency": 5.0},
    7:  {"uuid": A % 7,  "due": "2026-09-05", "priority": None, "urgency": 5.0},
    8:  {"uuid": A % 8,  "due": "2026-09-05T23:59", "priority": "L", "urgency": 2.0},
    9:  {"uuid": A % 9,  "due": "2026-09-06T00:01", "priority": "H", "urgency": 50.0},
    10: {"uuid": A % 10, "due": "2026-09-04", "priority": None, "urgency": 0.5},
    11: {"uuid": A % 11, "due": None, "priority": "H", "urgency": 7.0},
    12: {"uuid": A % 12, "due": None, "priority": None, "urgency": 7.0},
    13: {"uuid": A % 13, "due": "2026-01-02", "priority": "M", "urgency": 3.0},
    14: {"uuid": A % 14, "due": "2026-09-05T00:00", "priority": None, "urgency": 3.0},
}

# Derived by hand from docs/api.md "Canonical order", not from the code:
#   dates ascending (13 Jan, 10 the 4th, then the 5th, then 9 on the 6th);
#   inside the 5th, clocked times ascending (00:00, 08:30, 09:00, 23:59) and
#   then the date-only ones, whose "99:99" puts them after every clock;
#   priority breaks a tie (1 H before 2 M; 5 H, 6 L, 7 none);
#   uuid breaks the last one (2 aaaa… before 3 bbbb…, urgency ignored);
#   and every undated task is last, because "" + "T" starts with 'T' and every
#   dated key starts with a digit.
KEY_ORDER = [13, 10, 14, 4, 1, 2, 3, 8, 5, 6, 7, 9, 11, 12]


def test_order_key_orders_the_14_cases():
    got = sorted(CASES, key=lambda n: order_key(CASES[n]))
    assert got == KEY_ORDER


def test_order_key_is_total():
    """No two of the 14 share a key — the uuid tail is what guarantees it, and
    without it a re-render could swap two rows under the user's thumb (and
    break the ETag, which is a hash of the body)."""
    keys = [order_key(c) for c in CASES.values()]
    assert len(set(keys)) == len(keys)


def test_date_only_sorts_after_every_clocked_due_of_that_day():
    assert order_key(CASES[8]) < order_key(CASES[5])      # 23:59 before "99:99"
    assert order_key(CASES[5]) < order_key(CASES[9])      # …but before tomorrow


# --------------------------------------------------------------------------- #
# sort_key — the four modes (docs/api.md "Ordering moves to the server")
# --------------------------------------------------------------------------- #
def grouped(n, group="today", **over):
    t = dict(CASES[n], group=group)
    t.update(over)
    return t


def test_group_always_leads_whatever_the_mode():
    rows = [grouped(1, "none"), grouped(2, "upcoming"),
            grouped(3, "overdue"), grouped(4, "today")]
    for mode in ("due", "priority", "urgency", "manual"):
        got = [t["group"] for t in display_sort(rows, mode)]
        assert got == ["overdue", "today", "upcoming", "none"], mode


def test_due_mode_is_the_canonical_key():
    rows = [grouped(n) for n in (5, 4, 1, 8)]
    assert [t["uuid"] for t in display_sort(rows, "due")] == \
           [CASES[n]["uuid"] for n in (4, 1, 8, 5)]


def test_priority_mode_puts_priority_first():
    #  4 has no priority but the earliest clock; in "due" it leads, here it trails.
    rows = [grouped(n) for n in (4, 1, 6, 2)]
    assert [t["uuid"] for t in display_sort(rows, "priority")] == \
           [CASES[n]["uuid"] for n in (1, 2, 6, 4)]


def test_urgency_mode_is_urgency_then_uuid():
    rows = [grouped(n) for n in (4, 3, 9, 1)]
    assert [t["uuid"] for t in display_sort(rows, "urgency")] == \
           [CASES[n]["uuid"] for n in (3, 9, 1, 4)]      # 99, 50, 10, 1


def test_no_date_group_leads_with_urgency():
    """docs/api.md "Canonical order" 3 — there is no date to lead with."""
    rows = [grouped(11, "none", urgency=1.0), grouped(12, "none", urgency=9.0)]
    # 11 is priority H and would win on the key alone; urgency outranks it here.
    assert [t["uuid"] for t in display_sort(rows, "due")] == \
           [CASES[12]["uuid"], CASES[11]["uuid"]]


def test_manual_mode_places_ordered_tasks_first_ascending():
    rows = [grouped(1, order=None), grouped(4, order=30.0),
            grouped(8, order=10.0), grouped(5, order=20.5)]
    assert [t["uuid"] for t in display_sort(rows, "manual")] == \
           [CASES[n]["uuid"] for n in (8, 5, 4, 1)]


def test_other_modes_ignore_order():
    """A dragged arrangement survives a trip through "due" and back."""
    rows = [grouped(1, order=99.0), grouped(4, order=1.0)]
    assert [t["uuid"] for t in display_sort(rows, "due")] == \
           [CASES[4]["uuid"], CASES[1]["uuid"]]          # the canonical key wins
    assert sort_key(rows[0], "urgency") < sort_key(rows[1], "urgency")


# --------------------------------------------------------------------------- #
# End to end, through the API and the real CLI
# --------------------------------------------------------------------------- #
async def test_list_carries_group_and_order_on_every_task(client):
    await make(client, "dated", due=day(3))
    await make(client, "undated")

    rows = await listing(client)
    for t in rows:
        assert t["group"] in ("overdue", "today", "upcoming", "none")
        assert "order" in t and t["order"] is None
    by_desc = {t["description"]: t for t in rows}
    assert by_desc["dated"]["group"] == "upcoming"
    assert by_desc["undated"]["group"] == "none"


async def test_list_is_in_display_order_not_urgency_order(client):
    """The overdue task with the *lowest* urgency still comes first."""
    await make(client, "urgent but later", priority="H", due=day(2))
    await make(client, "overdue and dull", due=day(-1))

    rows = await listing(client)
    assert [t["description"] for t in rows] == ["overdue and dull",
                                                "urgent but later"]
    assert rows[0]["urgency"] < rows[1]["urgency"]


async def test_group_boundaries_end_to_end(client):
    await make(client, "yesterday", due=day(-1))
    await make(client, "today date-only", due=day(0))
    await make(client, "today at 00:01", due="%sT00:01" % day(0))
    await make(client, "tomorrow", due=day(1))
    await make(client, "someday")

    groups = {t["description"]: t["group"] for t in await listing(client)}
    assert groups["yesterday"] == "overdue"
    # Date-only today is never overdue; a clocked 00:01 today is overdue by any
    # hour anyone runs this suite.
    assert groups["today date-only"] == "today"
    assert groups["today at 00:01"] == "overdue"
    assert groups["tomorrow"] == "upcoming"
    assert groups["someday"] == "none"


async def test_completed_list_is_untouched_by_the_sort_mode(client):
    """Done is newest-first and no mode reorders it."""
    first = await make(client, "first")
    second = await make(client, "second")
    await client.post("/api/tasks/%s/done" % first["uuid"])
    await client.post("/api/tasks/%s/done" % second["uuid"])
    task(first["uuid"], "modify", "end:%sT09:00" % day(-2))
    task(second["uuid"], "modify", "end:%sT09:00" % day(-1))

    for mode in ("due", "priority", "urgency", "manual"):
        await client.put("/api/prefs", json={"sort": {"mode": mode}})
        rows = await listing(client, "completed")
        assert [t["description"] for t in rows] == ["second", "first"], mode


async def test_sort_mode_changes_the_order(client):
    """Same three tasks, two prefs, two orders — all inside one group."""
    await make(client, "early no priority", due="%sT08:00" % day(2))
    await make(client, "late high priority", priority="H", due="%sT18:00" % day(2))

    await client.put("/api/prefs", json={"sort": {"mode": "due"}})
    assert [t["description"] for t in await listing(client)] == \
        ["early no priority", "late high priority"]

    await client.put("/api/prefs", json={"sort": {"mode": "priority"}})
    assert [t["description"] for t in await listing(client)] == \
        ["late high priority", "early no priority"]


async def test_manual_mode_follows_the_order_field(client):
    a = await make(client, "a", due=day(2))
    b = await make(client, "b", due=day(2))
    c = await make(client, "c", due=day(2))

    await client.put("/api/prefs", json={"sort": {"mode": "manual"}})
    # With nothing placed, "manual" is the canonical key — three tasks alike in
    # date and priority, so it is uuid order and c is not first.
    canonical = [t["description"] for t in await listing(client)]
    assert sorted(canonical) == ["a", "b", "c"]

    await client.patch("/api/tasks/%s" % c["uuid"], json={"order": 1000})
    await client.patch("/api/tasks/%s" % a["uuid"], json={"order": 2000})
    rows = await listing(client)
    assert [t["description"] for t in rows] == ["c", "a", "b"]   # b unplaced, last
    assert rows[0]["order"] == 1000 and rows[1]["order"] == 2000
    assert rows[2]["order"] is None

    # Switching modes leaves the arrangement in Taskwarrior untouched.
    await client.put("/api/prefs", json={"sort": {"mode": "due"}})
    assert {t["description"]: t["order"] for t in await listing(client)} == \
        {"a": 2000, "b": None, "c": 1000}


# --------------------------------------------------------------------------- #
# PATCH order
# --------------------------------------------------------------------------- #
async def test_order_round_trips_int_float_negative_and_null(client):
    t = await make(client, "draggable")
    url = "/api/tasks/%s" % t["uuid"]

    for value in (1000, 1500.5, -3, 0):
        got = (await client.patch(url, json={"order": value})).json()
        assert got["order"] == value, value
        assert got["description"] == "draggable"          # not "order:1000"

    assert (await client.patch(url, json={"order": None})).json()["order"] is None


async def test_order_is_written_to_the_uda_not_the_description(client):
    """The failure this guards is silent: with the UDA undeclared, Taskwarrior
    takes `order:1500` for description text and exits 0."""
    t = await make(client, "keep my words")
    await client.patch("/api/tasks/%s" % t["uuid"], json={"order": 1500})

    raw = task(t["uuid"], "export").stdout
    assert '"order":1500' in raw.replace(" ", "")
    assert (await client.get("/api/tasks/%s" % t["uuid"])).json()["description"] \
        == "keep my words"


async def test_order_rejects_non_numbers(client):
    t = await make(client, "x")
    for bad in ("1000", True, [1], {"a": 1}):
        r = await client.patch("/api/tasks/%s" % t["uuid"], json={"order": bad})
        assert r.status_code == 422, bad
        assert r.json()["detail"].startswith("order:")


def test_order_rejects_infinity():
    """Not through HTTP: httpx will not encode a non-finite float, so this one
    is checked where it is decided. `json.loads` *does* accept `Infinity`, so
    the guard is not theoretical — a hand-rolled client can send it."""
    import pytest

    from app.schemas import clean_order

    for bad in (float("inf"), float("-inf"), float("nan"), 1e13):
        with pytest.raises(ValueError):
            clean_order(bad)


async def test_order_survives_an_unrelated_patch(client):
    t = await make(client, "x")
    await client.patch("/api/tasks/%s" % t["uuid"], json={"order": 42})
    got = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"priority": "H"})).json()
    assert got["order"] == 42 and got["priority"] == "H"


async def test_etag_changes_when_only_the_sort_mode_does(client):
    """The body is the order, so the phone's 30-second poll notices a pref
    change without anything having to push it one."""
    await make(client, "early", due="%sT08:00" % day(2))
    await make(client, "late", priority="H", due="%sT18:00" % day(2))

    before = (await client.get("/api/tasks")).headers["etag"]
    await client.put("/api/prefs", json={"sort": {"mode": "priority"}})
    assert (await client.get("/api/tasks")).headers["etag"] != before


async def test_order_is_refused_when_the_uda_is_not_declared(client, monkeypatch):
    """The guard that stops a drag from shredding descriptions.

    With `uda.order.type` absent from the taskrc, `task <uuid> modify
    order:1500` exits **0** and sets the task's DESCRIPTION to "order:1500" —
    the token is not an attribute Taskwarrior knows, so it falls through to the
    text. Verified below against the real binary, which is why the write is
    refused rather than attempted.
    """
    import os
    from pathlib import Path

    from .conftest import DATA, RC

    bare = RC.parent / "taskrc-no-uda"
    bare.write_text("data.location=%s\nhooks=off\nconfirmation=off\n" % DATA)

    t = await make(client, "keep my words")
    monkeypatch.setenv("TASKRC", str(bare))

    r = await client.patch("/api/tasks/%s" % t["uuid"], json={"order": 1500})
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert "uda.order.type=numeric" in r.json()["detail"]

    # Nothing was written, and this is what would have happened without it:
    assert (await client.get("/api/tasks/%s"
                             % t["uuid"])).json()["description"] == "keep my words"
    assert task(t["uuid"], "modify", "order:1500").returncode == 0
    assert (await client.get("/api/tasks/%s"
                             % t["uuid"])).json()["description"] == "order:1500"
    assert Path(os.environ["TASKRC"]) == bare


async def test_clearing_order_is_refused_too(client, monkeypatch):
    """`order:` is no safer than `order:1500`: undeclared, it sets the
    description to the literal string "order:". So the guard covers the clear
    as well, and the price is that an offline queue holding one cannot drain
    until the taskrc is fixed — which is the right way round."""
    from .conftest import DATA, RC

    bare = RC.parent / "taskrc-no-uda"
    bare.write_text("data.location=%s\nhooks=off\nconfirmation=off\n" % DATA)
    t = await make(client, "x")
    monkeypatch.setenv("TASKRC", str(bare))

    r = await client.patch("/api/tasks/%s" % t["uuid"], json={"order": None})
    assert r.status_code == 409
    assert (await client.get("/api/tasks/%s" % t["uuid"])).json()["description"] == "x"

    assert task(t["uuid"], "modify", "order:").returncode == 0
    assert (await client.get("/api/tasks/%s"
                             % t["uuid"])).json()["description"] == "order:"


async def test_order_comes_back_as_the_number_that_was_sent(client):
    """Taskwarrior is inconsistent about this: the same `modify order:1500`
    exports `1500` on one database and `1500.0` on another (seen on the real
    box). The client sends an int and gets an int."""
    t = await make(client, "x")
    body = (await client.patch("/api/tasks/%s" % t["uuid"],
                               json={"order": 1500})).json()
    assert isinstance(body["order"], int) and body["order"] == 1500

    body = (await client.patch("/api/tasks/%s" % t["uuid"],
                               json={"order": 1250.5})).json()
    assert body["order"] == 1250.5
