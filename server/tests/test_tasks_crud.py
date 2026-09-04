"""Create, read, update, complete, delete, annotate — against the real CLI."""
from __future__ import annotations

from .conftest import make, task


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
async def test_create_minimal(client):
    t = await make(client, "  call bob  ")
    assert t["description"] == "call bob"            # trimmed
    assert t["status"] == "pending"
    assert t["project"] is None and t["priority"] is None
    assert t["due"] is None and t["due_at"] is None
    assert t["tags"] == [] and t["annotations"] == []
    assert t["parent"] is None and t["recur"] is None
    assert t["depends"] == [] and t["blocked"] is False
    assert t["end"] is None
    assert len(t["uuid"]) == 36
    assert "id" not in t                             # deliberately absent


async def test_create_with_everything(client):
    t = await make(client, "ship it", project="work", priority="H",
                   due="2026-09-05T14:30", tags=["claude", "alert"])
    assert t["project"] == "work"
    assert t["priority"] == "H"
    assert t["due"] == "2026-09-05T14:30"
    assert t["due_at"] == "2026-09-05T14:30:00-05:00"
    assert sorted(t["tags"]) == ["alert", "claude"]


async def test_description_keeps_taskwarrior_syntax_literal(client):
    """The whole reason `task add` gets a `--`: this must not become a due date."""
    t = await make(client, "call bob due:tomorrow +urgent project:work")
    assert t["description"] == "call bob due:tomorrow +urgent project:work"
    assert t["due"] is None
    assert t["tags"] == []
    assert t["project"] is None


async def test_description_survives_shell_metacharacters(client):
    nasty = "rm -rf $(echo x); `id` && echo \"pwned\" | tee /tmp/x"
    t = await make(client, nasty)
    assert t["description"] == nasty


async def test_description_keeps_newlines(client):
    t = await make(client, "line one\nline two")
    assert t["description"] == "line one\nline two"


async def test_reserved_tags_are_dropped_on_create(client):
    t = await make(client, "x", tags=["claude", "today", "overdue", "due"])
    assert t["tags"] == ["claude"]


# --------------------------------------------------------------------------- #
# Both due shapes round-trip (docs/design.md D3)
# --------------------------------------------------------------------------- #
async def test_date_only_due_round_trips(client):
    t = await make(client, "digest only", due="2026-09-05")
    assert t["due"] == "2026-09-05"                  # no clock => date-only
    assert t["due_at"] == "2026-09-05T00:00:00-05:00"
    # …and Taskwarrior stored local midnight, which is 05:00Z in CDT.
    raw = task(t["uuid"], "export").stdout
    assert "20260905T050000Z" in raw


async def test_clocked_due_round_trips(client):
    t = await make(client, "pings", due="2026-09-05T14:30")
    assert t["due"] == "2026-09-05T14:30"
    raw = task(t["uuid"], "export").stdout
    assert "20260905T193000Z" in raw                 # 14:30 CDT

    again = (await client.get("/api/tasks/%s" % t["uuid"])).json()
    assert again["due"] == "2026-09-05T14:30"


async def test_winter_due_uses_cst_offset(client):
    """The offset is computed, not hard-coded: January is CST (-06:00)."""
    t = await make(client, "winter", due="2027-01-15T08:00")
    assert t["due"] == "2027-01-15T08:00"
    assert t["due_at"] == "2027-01-15T08:00:00-06:00"


# --------------------------------------------------------------------------- #
# Read one
# --------------------------------------------------------------------------- #
async def test_get_one(client):
    t = await make(client, "findable")
    r = await client.get("/api/tasks/%s" % t["uuid"])
    assert r.status_code == 200
    assert r.json()["uuid"] == t["uuid"]


async def test_get_unknown_uuid_is_404(client):
    r = await client.get("/api/tasks/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "detail": "No task with that uuid."}


async def test_get_non_uuid_is_404_not_a_filter(client):
    """`status:pending` as a path parameter must not become a Taskwarrior filter."""
    await make(client, "safe")
    for junk in ("status:pending", "1", "abcd", "+claude"):
        r = await client.get("/api/tasks/%s" % junk)
        assert r.status_code == 404, junk


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #
async def test_patch_sets_fields(client):
    t = await make(client, "before")
    r = await client.patch("/api/tasks/%s" % t["uuid"], json={
        "description": "after", "project": "work", "priority": "M",
        "due": "2026-09-09", "tags": ["claude"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["description"] == "after"
    assert out["project"] == "work"
    assert out["priority"] == "M"
    assert out["due"] == "2026-09-09"
    assert out["tags"] == ["claude"]


async def test_patch_null_clears_and_omitted_is_untouched(client):
    t = await make(client, "full", project="work", priority="H",
                   due="2026-09-05T14:30", tags=["claude"])

    out = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"due": None})).json()
    assert out["due"] is None and out["due_at"] is None
    assert out["project"] == "work"                  # untouched
    assert out["priority"] == "H"
    assert out["tags"] == ["claude"]

    out = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"project": None, "priority": None})).json()
    assert out["project"] is None and out["priority"] is None
    assert out["description"] == "full"


async def test_patch_tags_replace_and_never_touch_reserved(client):
    t = await make(client, "tagged", tags=["claude", "alert"])
    # `pa retag` paints its maintenance tags on behind our back.
    task(t["uuid"], "modify", "+today", "+overdue", "+due")

    out = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"tags": ["alert", "next"]})).json()
    assert sorted(out["tags"]) == ["alert", "next"]   # claude removed, next added

    raw = task(t["uuid"], "export").stdout
    for reserved in ("today", "overdue", "due"):
        assert '"%s"' % reserved in raw               # still on the real task


async def test_patch_tags_empty_list_clears_user_tags_only(client):
    t = await make(client, "tagged", tags=["claude"])
    task(t["uuid"], "modify", "+today")

    out = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"tags": []})).json()
    assert out["tags"] == []
    assert '"today"' in task(t["uuid"], "export").stdout


async def test_patch_ignores_reserved_tags_sent_by_the_client(client):
    t = await make(client, "tagged", tags=["claude"])
    out = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"tags": ["claude", "today"]})).json()
    assert out["tags"] == ["claude"]
    assert '"today"' not in task(t["uuid"], "export").stdout


async def test_patch_description_stays_literal(client):
    t = await make(client, "before")
    out = (await client.patch("/api/tasks/%s" % t["uuid"],
                              json={"description": "after due:tomorrow +x"})).json()
    assert out["description"] == "after due:tomorrow +x"
    assert out["due"] is None and out["tags"] == []


async def test_patch_unknown_uuid_is_404(client):
    r = await client.patch("/api/tasks/00000000-0000-0000-0000-000000000000",
                           json={"priority": "H"})
    assert r.status_code == 404


async def test_patch_empty_body_is_a_no_op(client):
    t = await make(client, "unchanged", project="work")
    out = (await client.patch("/api/tasks/%s" % t["uuid"], json={})).json()
    assert out["description"] == "unchanged" and out["project"] == "work"


# --------------------------------------------------------------------------- #
# done / undone
# --------------------------------------------------------------------------- #
async def test_done_then_undone(client):
    t = await make(client, "finish me")

    out = (await client.post("/api/tasks/%s/done" % t["uuid"])).json()
    assert out["status"] == "completed"
    assert out["end"] is not None

    out = (await client.post("/api/tasks/%s/undone" % t["uuid"])).json()
    assert out["status"] == "pending"
    assert out["end"] is None                        # modify status:pending clears it


async def test_done_is_idempotent(client):
    t = await make(client, "twice")
    assert (await client.post("/api/tasks/%s/done" % t["uuid"])).status_code == 200
    r = await client.post("/api/tasks/%s/done" % t["uuid"])
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


async def test_undone_on_a_pending_task_is_409(client):
    t = await make(client, "still pending")
    r = await client.post("/api/tasks/%s/undone" % t["uuid"])
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"


async def test_done_unknown_uuid_is_404(client):
    r = await client.post("/api/tasks/00000000-0000-0000-0000-000000000000/done")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
async def test_delete_keeps_the_record(client):
    t = await make(client, "goodbye")
    r = await client.delete("/api/tasks/%s" % t["uuid"])
    assert r.status_code == 204
    assert r.content == b""

    again = await client.get("/api/tasks/%s" % t["uuid"])
    assert again.status_code == 200
    assert again.json()["status"] == "deleted"
    # …but it is gone from the pending list.
    assert (await client.get("/api/tasks")).json() == []


async def test_delete_is_idempotent(client):
    t = await make(client, "goodbye")
    assert (await client.delete("/api/tasks/%s" % t["uuid"])).status_code == 204
    assert (await client.delete("/api/tasks/%s" % t["uuid"])).status_code == 204


async def test_delete_unknown_uuid_is_404(client):
    r = await client.delete("/api/tasks/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# annotate
# --------------------------------------------------------------------------- #
async def test_annotate(client):
    t = await make(client, "needs a note")
    r = await client.post("/api/tasks/%s/annotations" % t["uuid"],
                          json={"text": "  claude: looked into it  "})
    assert r.status_code == 200
    notes = r.json()["annotations"]
    assert len(notes) == 1
    assert notes[0]["text"] == "claude: looked into it"
    assert notes[0]["entry"].endswith(("-05:00", "-06:00"))

    r = await client.post("/api/tasks/%s/annotations" % t["uuid"],
                          json={"text": "second"})
    assert [n["text"] for n in r.json()["annotations"]] == \
        ["claude: looked into it", "second"]


async def test_annotate_empty_is_422(client):
    t = await make(client, "x")
    r = await client.post("/api/tasks/%s/annotations" % t["uuid"],
                          json={"text": "   "})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_request"
    assert "text" in r.json()["detail"]


async def test_annotate_unknown_uuid_is_404(client):
    r = await client.post(
        "/api/tasks/00000000-0000-0000-0000-000000000000/annotations",
        json={"text": "hi"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #
async def test_blocked_reflects_a_pending_dependency(client):
    a = await make(client, "blocker")
    b = await make(client, "blocked one")
    task(b["uuid"], "modify", "depends:%s" % a["uuid"])

    rows = {t["description"]: t for t in (await client.get("/api/tasks")).json()}
    assert rows["blocked one"]["depends"] == [a["uuid"]]
    assert rows["blocked one"]["blocked"] is True
    assert rows["blocker"]["blocked"] is False

    await client.post("/api/tasks/%s/done" % a["uuid"])
    rows = {t["description"]: t for t in (await client.get("/api/tasks")).json()}
    assert rows["blocked one"]["blocked"] is False


# --------------------------------------------------------------------------- #
# recurrence
# --------------------------------------------------------------------------- #
async def test_recurring_instances_are_listed_but_templates_are_not(client):
    """Mirrors a real daily task: the template is status:recurring and stays off
    the phone; its pending instance carries parent + recur."""
    task("add", "recur:daily", "due:today", "--", "water the plants")

    rows = (await client.get("/api/tasks")).json()
    assert rows, "the pending instance should be listed"
    for inst in rows:
        assert inst["description"] == "water the plants"
        assert inst["status"] == "pending"          # never "recurring"
        assert inst["parent"] is not None           # …it is an instance
        assert inst["recur"] == "daily"

    # The template itself carries status:recurring and stays off the phone.
    parents = {r["parent"] for r in rows}
    assert parents & {t["uuid"] for t in rows} == set()
