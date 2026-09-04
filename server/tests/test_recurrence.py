"""PATCH recur / until, against the real Taskwarrior 3.4.2.

Taskwarrior's recurrence model is a `status:recurring` **template** plus one
pending **instance** at a time (`recurrence.limit=1`). The phone only ever sees
instances, so every one of these endpoints is really "find the task that owns
the schedule and write there instead".

Half of what the contract originally described turns out to be impossible on
3.4.2, and the tests that pin that down are the point of this file — see
`test_taskwarrior_refuses_to_clear_recur_on_an_instance` and
`test_clearing_parent_would_make_a_new_template` for the two commands the
obvious implementation would have used.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .conftest import listing, make, task

CHI = ZoneInfo("America/Chicago")


def day(offset: int) -> str:
    return (datetime.now(CHI) + timedelta(days=offset)).strftime("%Y-%m-%d")


def export(*filters: str) -> list:
    import json
    out = task(*filters, "export").stdout.strip()
    return json.loads(out) if out else []


async def make_repeating(client, description="water plants", recur="daily",
                         due=None):
    """A plain task promoted to a template. Returns the PATCH response, which
    the contract says is the FIRST INSTANCE, not the task that was sent."""
    plain = await make(client, description, due=due or day(1))
    r = await client.patch("/api/tasks/%s" % plain["uuid"],
                           json={"recur": recur})
    assert r.status_code == 200, r.text
    return plain, r.json()


# --------------------------------------------------------------------------- #
# Plain task -> template
# --------------------------------------------------------------------------- #
async def test_plain_task_becomes_a_template_and_answers_with_the_instance(client):
    plain, instance = await make_repeating(client)

    # The task that was PATCHed is now the template; the response is the
    # instance Taskwarrior spawned from it, with a different uuid.
    assert instance["uuid"] != plain["uuid"]
    assert instance["parent"] == plain["uuid"]
    assert instance["status"] == "pending"
    assert instance["recur"] == "daily"
    assert instance["description"] == "water plants"

    template = (await client.get("/api/tasks/%s" % plain["uuid"])).json()
    assert template["status"] == "recurring"


async def test_the_template_is_not_in_the_list_but_the_instance_is(client):
    plain, instance = await make_repeating(client)
    uuids = [t["uuid"] for t in await listing(client)]
    assert uuids == [instance["uuid"]]
    assert plain["uuid"] not in uuids


async def test_recurrence_without_a_due_is_422(client):
    t = await make(client, "someday")
    r = await client.patch("/api/tasks/%s" % t["uuid"], json={"recur": "daily"})
    assert r.status_code == 422
    assert r.json() == {"error": "invalid_request",
                        "detail": "recur: recurrence needs a due date"}
    # And nothing happened — Taskwarrior would have exited 2 here anyway
    # ("You cannot specify a recurring task without a due date."), but a 502 is
    # not what a missing field deserves.
    assert (await client.get("/api/tasks/%s" % t["uuid"])).json()["status"] \
        == "pending"


async def test_a_due_sent_in_the_same_patch_satisfies_the_check(client):
    t = await make(client, "someday")
    r = await client.patch("/api/tasks/%s" % t["uuid"],
                           json={"due": day(2), "recur": "weekly"})
    assert r.status_code == 200, r.text
    assert r.json()["recur"] == "weekly"
    assert r.json()["due"] == day(2)


async def test_an_unsupported_period_is_the_clis_own_complaint(client):
    t = await make(client, "x", due=day(1))
    r = await client.patch("/api/tasks/%s" % t["uuid"],
                           json={"recur": "bogusly"})
    assert r.status_code == 502
    assert r.json()["error"] == "task_failed"
    assert "not supported" in r.json()["detail"]


async def test_a_malformed_period_never_reaches_the_cli(client):
    t = await make(client, "x", due=day(1))
    for bad in ("2 weeks", "daily; rm -rf /", "+7d", "x" * 30):
        r = await client.patch("/api/tasks/%s" % t["uuid"], json={"recur": bad})
        assert r.status_code == 422, bad
        assert r.json()["detail"].startswith("recur:")


# --------------------------------------------------------------------------- #
# Editing an instance edits its template
# --------------------------------------------------------------------------- #
async def test_patching_recur_on_an_instance_reaches_the_template(client):
    plain, instance = await make_repeating(client, recur="daily")

    r = await client.patch("/api/tasks/%s" % instance["uuid"],
                           json={"recur": "weekly"})
    assert r.status_code == 200
    assert r.json()["uuid"] == instance["uuid"]          # same row, not a new one
    assert r.json()["recur"] == "weekly"

    raw_template = export(plain["uuid"])[0]
    assert raw_template["recur"] == "weekly"


async def test_recur_is_reported_from_the_template_not_the_instance(client):
    """3.4.2 does NOT propagate a template edit to the instance already spawned.

    The instance's own `recur` is a snapshot from when it was created, so
    reading it off the row would show the user the schedule they just replaced.
    """
    plain, instance = await make_repeating(client, recur="daily")
    task(plain["uuid"], "modify", "recur:weekly")        # straight to the CLI

    assert export(instance["uuid"])[0]["recur"] == "daily"        # stale, really
    assert (await client.get("/api/tasks/%s"
                             % instance["uuid"])).json()["recur"] == "weekly"


async def test_until_round_trips_through_the_template(client):
    plain, instance = await make_repeating(client)
    assert instance["until"] is None

    r = await client.patch("/api/tasks/%s" % instance["uuid"],
                           json={"until": "2026-12-01"})
    assert r.json()["until"] == "2026-12-01"
    assert export(plain["uuid"])[0]["until"].startswith("20261201")
    # Same trap as `recur`: the instance itself never learned about it.
    assert "until" not in export(instance["uuid"])[0]

    cleared = await client.patch("/api/tasks/%s" % instance["uuid"],
                                 json={"until": None})
    assert cleared.json()["until"] is None
    assert "until" not in export(plain["uuid"])[0]


async def test_recur_and_until_can_be_set_together_on_a_plain_task(client):
    t = await make(client, "gym", due=day(1))
    r = await client.patch("/api/tasks/%s" % t["uuid"],
                           json={"recur": "weekly", "until": "2026-12-31"})
    assert r.status_code == 200
    body = r.json()
    assert body["recur"] == "weekly" and body["until"] == "2026-12-31"


async def test_until_is_a_date_shape(client):
    plain, instance = await make_repeating(client)
    r = await client.patch("/api/tasks/%s" % instance["uuid"],
                           json={"until": "next tuesday"})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("until:")


# --------------------------------------------------------------------------- #
# Stop repeating — and exactly what survives on 3.4.2
# --------------------------------------------------------------------------- #
async def test_stop_repeating_deletes_the_template_and_keeps_the_instance(client):
    plain, instance = await make_repeating(client)

    r = await client.patch("/api/tasks/%s" % instance["uuid"],
                           json={"recur": None})
    assert r.status_code == 200
    body = r.json()

    # The row the user was looking at is still there, same uuid, same due, and
    # it now presents as an ordinary task.
    assert body["uuid"] == instance["uuid"]
    assert body["status"] == "pending"
    assert body["recur"] is None and body["parent"] is None and body["until"] is None
    assert [t["uuid"] for t in await listing(client)] == [instance["uuid"]]

    # The template is gone.
    assert export(plain["uuid"])[0]["status"] == "deleted"


async def test_stop_repeating_really_stops_it(client):
    """The behavioural claim, not the cosmetic one: completing the surviving
    instance must not spawn another."""
    plain, instance = await make_repeating(client)
    await client.patch("/api/tasks/%s" % instance["uuid"], json={"recur": None})

    await client.post("/api/tasks/%s/done" % instance["uuid"])
    assert await listing(client) == []


async def test_what_taskwarrior_leaves_behind_is_documented(client):
    """The residue the API hides: on 3.4.2 the instance keeps `recur` and a
    `parent` pointing at the deleted template, forever. Nothing can clear them
    (see the two tests below), so `serialize.task_out` reports a row whose
    template is gone as the plain task it now behaves like."""
    plain, instance = await make_repeating(client)
    await client.patch("/api/tasks/%s" % instance["uuid"], json={"recur": None})

    raw = export(instance["uuid"])[0]
    assert raw["recur"] == "daily"                       # still there in the DB
    assert raw["parent"] == plain["uuid"]                # pointing at a deleted task
    assert raw["status"] == "pending"


def test_taskwarrior_refuses_to_clear_recur_on_an_instance():
    """`modify recur:` — the contract's first suggestion. Exit 2, no change."""
    task("add", "due:%s" % day(1), "recur:daily", "--", "gym")
    instance = [t for t in export("status:pending") if t.get("parent")][0]

    res = task(instance["uuid"], "modify", "recur:")
    assert res.returncode != 0
    assert "cannot remove the recurrence" in (res.stdout + res.stderr)
    assert export(instance["uuid"])[0]["recur"] == "daily"


def test_clearing_parent_would_make_a_new_template():
    """`modify parent:` — the contract's second suggestion, and the dangerous
    one. It succeeds, and it promotes the instance into a live template that
    immediately spawns a fresh instance: the row the user asked to KEEP leaves
    the pending list, and the thing they asked to STOP carries on."""
    task("add", "due:%s" % day(1), "recur:daily", "--", "gym")
    instance = [t for t in export("status:pending") if t.get("parent")][0]
    template = instance["parent"]
    task(template, "delete")

    assert task(instance["uuid"], "modify", "parent:").returncode == 0

    after = export(instance["uuid"])[0]
    assert after["status"] == "recurring"                # it is a template now
    spawned = [t for t in export("status:pending")
               if t.get("parent") == instance["uuid"]]
    assert len(spawned) == 1                            # and it repeats again


async def test_stop_repeating_is_a_no_op_on_a_plain_task(client):
    """A replayed offline write must settle, not raise (design.md D6)."""
    t = await make(client, "ordinary", due=day(1))
    r = await client.patch("/api/tasks/%s" % t["uuid"], json={"recur": None})
    assert r.status_code == 200
    assert r.json()["uuid"] == t["uuid"] and r.json()["status"] == "pending"


async def test_stop_repeating_twice_settles(client):
    plain, instance = await make_repeating(client)
    first = await client.patch("/api/tasks/%s" % instance["uuid"],
                               json={"recur": None})
    second = await client.patch("/api/tasks/%s" % instance["uuid"],
                                json={"recur": None})
    assert first.status_code == second.status_code == 200
    assert second.json()["recur"] is None
    assert [t["uuid"] for t in await listing(client)] == [instance["uuid"]]


async def test_a_stopped_task_can_be_made_to_repeat_again(client):
    """The other half of the orphan problem: `modify recur:daily` on a task
    that still carries a dead `parent` writes the field and spawns NOTHING, so
    the promotion path has to clear `parent`/`imask` first."""
    plain, instance = await make_repeating(client, recur="daily")
    await client.patch("/api/tasks/%s" % instance["uuid"], json={"recur": None})

    r = await client.patch("/api/tasks/%s" % instance["uuid"],
                           json={"recur": "weekly"})
    assert r.status_code == 200
    again = r.json()
    assert again["uuid"] != instance["uuid"]             # a fresh instance
    assert again["parent"] == instance["uuid"]           # of the ex-instance
    assert again["recur"] == "weekly"
    assert [t["uuid"] for t in await listing(client)] == [again["uuid"]]


# --------------------------------------------------------------------------- #
# The rest of the Task object still works on an instance
# --------------------------------------------------------------------------- #
async def test_ordinary_fields_still_patch_on_an_instance(client):
    plain, instance = await make_repeating(client)
    r = await client.patch("/api/tasks/%s" % instance["uuid"],
                           json={"priority": "H", "project": "personal",
                                 "tags": ["claude"]})
    assert r.status_code == 200
    body = r.json()
    assert body["priority"] == "H" and body["project"] == "personal"
    assert body["tags"] == ["claude"]
    assert body["recur"] == "daily"                      # still repeating


async def test_until_appears_on_every_task_even_plain_ones(client):
    t = await make(client, "plain")
    assert t["until"] is None
    assert "until" in (await listing(client))[0]


async def test_periods_are_passed_through_with_their_case_intact(client):
    """3.4.2's periods are case-sensitive in BOTH directions — `P1M` works and
    `p1m` does not; `weekly` works and `Weekly` does not — so the server folds
    neither and lets the CLI judge."""
    for period in ("daily", "weekdays", "weekly", "biweekly", "monthly",
                   "yearly", "3days", "2w", "P1M", "P10D"):
        t = await make(client, "x %s" % period, due=day(1))
        r = await client.patch("/api/tasks/%s" % t["uuid"], json={"recur": period})
        assert r.status_code == 200, (period, r.text)
        assert r.json()["recur"] == period

    for period in ("p1m", "Weekly"):
        t = await make(client, "y %s" % period, due=day(1))
        r = await client.patch("/api/tasks/%s" % t["uuid"], json={"recur": period})
        assert r.status_code == 502, period
        assert period in r.json()["detail"]
