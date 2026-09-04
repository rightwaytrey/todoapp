"""GET /api/widget — the pre-drawn feed (docs/design.md D14, api.md round 5).

Everything the widget used to work out in Swift is asserted here instead: which
tasks appear, in what order, and what the due label says. The extension keeps
one job, which is drawing them.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.serialize import due_label

from .conftest import make

CHI = ZoneInfo("America/Chicago")


def day(offset: int) -> str:
    return (datetime.now(CHI) + timedelta(days=offset)).strftime("%Y-%m-%d")


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CHI)


async def feed(client) -> dict:
    r = await client.get("/api/widget")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
async def test_shape_and_caps(client):
    await make(client, "due now", due=day(-1))
    body = await feed(client)

    assert set(body) == {"updated", "total", "caps", "rows"}
    assert body["updated"].endswith(("-05:00", "-06:00"))
    assert body["total"] == 1
    # `caps`, NOT `rows` — `rows` is the array. The widget truncates to
    # caps.small / caps.medium for the smaller families.
    assert body["caps"] == {"small": 3, "medium": 5, "large": 12}
    assert set(body["rows"][0]) == {"uuid", "text", "due", "overdue", "group",
                                    "category"}


async def test_an_empty_feed_is_not_an_error(client):
    body = await feed(client)
    assert body["total"] == 0 and body["rows"] == []


async def test_text_is_flattened_to_one_line(client):
    await make(client, "line one\nline    two\n\nline three", due=day(-1))
    assert (await feed(client))["rows"][0]["text"] == "line one line two line three"


# --------------------------------------------------------------------------- #
# Which tasks (prefs.widget.groups / upcoming_days / category)
# --------------------------------------------------------------------------- #
async def test_default_groups_are_overdue_and_today_only(client):
    await make(client, "late", due=day(-1))
    await make(client, "now", due=day(0))
    await make(client, "later", due=day(3))
    await make(client, "someday")

    rows = (await feed(client))["rows"]
    assert [r["text"] for r in rows] == ["late", "now"]
    assert [r["group"] for r in rows] == ["overdue", "today"]
    assert [r["overdue"] for r in rows] == [True, False]


async def test_groups_can_be_widened_to_upcoming_and_none(client):
    await make(client, "late", due=day(-1))
    await make(client, "later", due=day(3))
    await make(client, "someday")

    await client.put("/api/prefs", json={
        "widget": {"groups": ["overdue", "upcoming", "none"]}})
    rows = (await feed(client))["rows"]
    assert [r["text"] for r in rows] == ["late", "later", "someday"]


async def test_upcoming_days_is_the_window(client):
    await make(client, "in three", due=day(3))
    await make(client, "in ten", due=day(10))

    await client.put("/api/prefs", json={
        "widget": {"groups": ["upcoming"], "upcoming_days": 7}})
    assert [r["text"] for r in (await feed(client))["rows"]] == ["in three"]

    await client.put("/api/prefs", json={
        "widget": {"groups": ["upcoming"], "upcoming_days": 14}})
    assert [r["text"] for r in (await feed(client))["rows"]] == ["in three",
                                                                "in ten"]


async def test_the_window_edge_is_a_date_not_an_hour(client):
    """"within 7 days" is "on or before the date 7 days out", so a task due at
    09:00 on the seventh day is in all day and does not fall out at lunchtime."""
    await make(client, "edge", due="%sT09:00" % day(7))
    await client.put("/api/prefs", json={
        "widget": {"groups": ["upcoming"], "upcoming_days": 7}})
    assert [r["text"] for r in (await feed(client))["rows"]] == ["edge"]


async def test_upcoming_days_does_not_filter_the_other_groups(client):
    await make(client, "late", due=day(-30))
    await client.put("/api/prefs", json={
        "widget": {"groups": ["overdue", "upcoming"], "upcoming_days": 1}})
    assert [r["text"] for r in (await feed(client))["rows"]] == ["late"]


async def test_a_category_filter_narrows_the_feed(client):
    await make(client, "work thing", project="work", due=day(-1))
    await make(client, "home thing", project="personal", due=day(-1))

    await client.put("/api/prefs", json={"widget": {"category": "work"}})
    body = await feed(client)
    assert [r["text"] for r in body["rows"]] == ["work thing"]
    assert body["total"] == 1


async def test_a_category_filter_excludes_uncategorised_tasks(client):
    await make(client, "loose", due=day(-1))
    await client.put("/api/prefs", json={"widget": {"category": "work"}})
    assert (await feed(client))["rows"] == []


async def test_category_is_only_filled_when_show_category_is_on(client):
    """The widget draws the category whenever the string is non-empty, and has
    no other way to know the pref."""
    await make(client, "work thing", project="work", due=day(-1))
    assert (await feed(client))["rows"][0]["category"] == ""

    await client.put("/api/prefs", json={"widget": {"show_category": True}})
    assert (await feed(client))["rows"][0]["category"] == "work"


async def test_an_uncategorised_task_shows_an_empty_category(client):
    await make(client, "loose", due=day(-1))
    await client.put("/api/prefs", json={"widget": {"show_category": True}})
    assert (await feed(client))["rows"][0]["category"] == ""


# --------------------------------------------------------------------------- #
# How many, and in what order
# --------------------------------------------------------------------------- #
async def test_rows_are_capped_at_large_and_total_counts_the_rest(client):
    for i in range(9):
        await make(client, "task %d" % i, due=day(-1))
    await client.put("/api/prefs", json={"widget": {"rows": {"large": 4}}})

    body = await feed(client)
    assert len(body["rows"]) == 4
    assert body["total"] == 9                            # what "+N more" counts
    assert body["caps"]["large"] == 4


async def test_rows_are_in_the_canonical_order(client):
    await make(client, "date-only today", due=day(0))
    await make(client, "at 23:58", due="%sT23:58" % day(0))
    await make(client, "yesterday", due=day(-1))

    rows = (await feed(client))["rows"]
    # Overdue first; then within today, a clocked due before a date-only one.
    assert [r["text"] for r in rows] == ["yesterday", "at 23:58",
                                         "date-only today"]


async def test_the_sort_mode_reaches_the_widget_too(client):
    await make(client, "early", due="%sT23:57" % day(0))
    await make(client, "late high", priority="H", due="%sT23:58" % day(0))

    assert [r["text"] for r in (await feed(client))["rows"]] == ["early",
                                                                 "late high"]
    await client.put("/api/prefs", json={"sort": {"mode": "priority"}})
    assert [r["text"] for r in (await feed(client))["rows"]] == ["late high",
                                                                 "early"]


async def test_recurring_templates_never_reach_the_widget(client):
    t = await make(client, "gym", due=day(0))
    await client.patch("/api/tasks/%s" % t["uuid"], json={"recur": "daily"})
    rows = (await feed(client))["rows"]
    assert [r["text"] for r in rows] == ["gym"]          # the instance, once
    assert rows[0]["uuid"] != t["uuid"]


# --------------------------------------------------------------------------- #
# due_label — the string the widget prints, tested against a frozen clock
# --------------------------------------------------------------------------- #
NOW = at("2026-09-04 09:00")

LABELS = [
    (None, "none", "", "no due at all"),
    ("2026-09-03", "overdue", "overdue", "a date in the past"),
    ("2026-09-04T08:00", "overdue", "overdue", "past its time today"),
    ("2026-09-04", "today", "today", "date-only today"),
    ("2026-09-04T14:30", "today", "2:30 pm", "clocked later today"),
    ("2026-09-04T00:30", "today", "12:30 am", "midnight hour reads 12, not 0"),
    ("2026-09-04T12:00", "today", "12:00 pm", "noon is pm"),
    ("2026-09-05", "upcoming", "Tomorrow", "tomorrow says so"),
    ("2026-09-05T14:30", "upcoming", "Tomorrow · 2:30 pm", "tomorrow, clocked"),
    ("2026-09-10", "upcoming", "Thu Sep 10", "date-only carries the weekday"),
    ("2026-09-12T14:30", "upcoming", "Sep 12 · 2:30 pm",
     "a clock drops the weekday — the row is one line"),
    ("2027-01-02", "upcoming", "Sat Jan 2, 2027", "another year says which"),
]


def test_due_label_table():
    for due, group, expected, why in LABELS:
        assert due_label(due, group, NOW) == expected, "%r (%s)" % (due, why)


async def test_labels_end_to_end(client):
    await make(client, "late", due=day(-2))
    await make(client, "today", due=day(0))
    await make(client, "clocked", due="%sT23:59" % day(0))

    labels = {r["text"]: r["due"] for r in (await feed(client))["rows"]}
    assert labels["late"] == "overdue"
    assert labels["today"] == "today"
    assert labels["clocked"] == "11:59 pm"
