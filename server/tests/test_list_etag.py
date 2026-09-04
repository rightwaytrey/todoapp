"""GET /api/tasks: the three status views, the ordering, and the ETag/304 that
makes the phone's 30-second poll cheap (docs/api.md List)."""
from __future__ import annotations

from .conftest import listing, make, task


async def test_pending_is_the_default_and_has_no_filter(client):
    await make(client, "one")
    await make(client, "two", project="work")
    await make(client, "three", tags=["claude"])

    default = await listing(client)
    assert {t["description"] for t in default} == {"one", "two", "three"}
    assert default == await listing(client, "pending")


async def test_pending_is_sorted_by_urgency_desc(client):
    await make(client, "low")
    await make(client, "high", priority="H", due="2026-09-03")
    await make(client, "middle", priority="L")

    rows = await listing(client)
    urgencies = [t["urgency"] for t in rows]
    assert urgencies == sorted(urgencies, reverse=True)
    assert rows[0]["description"] == "high"


async def test_completed_window_and_ordering(client):
    old = await make(client, "ancient")
    new = await make(client, "recent")
    await client.post("/api/tasks/%s/done" % old["uuid"])
    await client.post("/api/tasks/%s/done" % new["uuid"])
    # Push one completion outside the 30-day window.
    task(old["uuid"], "modify", "end:2026-01-01T09:00")

    rows = await listing(client, "completed")
    assert [t["description"] for t in rows] == ["recent"]
    assert rows[0]["status"] == "completed"
    assert rows[0]["end"] is not None


async def test_all_puts_pending_first(client):
    p = await make(client, "still open")
    d = await make(client, "shut")
    await client.post("/api/tasks/%s/done" % d["uuid"])

    rows = await listing(client, "all")
    assert [t["description"] for t in rows] == ["still open", "shut"]
    assert rows[0]["uuid"] == p["uuid"]


async def test_deleted_tasks_are_in_no_list(client):
    t = await make(client, "gone")
    await client.delete("/api/tasks/%s" % t["uuid"])
    for status in ("pending", "completed", "all"):
        assert await listing(client, status) == []


async def test_bad_status_is_422_naming_the_field(client):
    r = await client.get("/api/tasks", params={"status": "banana"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_request"
    assert body["detail"].startswith("status:")


# --------------------------------------------------------------------------- #
# ETag
# --------------------------------------------------------------------------- #
async def test_etag_304_round_trip(client):
    await make(client, "one")

    first = await client.get("/api/tasks")
    etag = first.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')

    second = await client.get("/api/tasks", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


async def test_etag_changes_when_a_task_changes(client):
    t = await make(client, "one")
    etag = (await client.get("/api/tasks")).headers["etag"]

    await client.patch("/api/tasks/%s" % t["uuid"], json={"priority": "H"})
    r = await client.get("/api/tasks", headers={"If-None-Match": etag})
    assert r.status_code == 200
    assert r.headers["etag"] != etag


async def test_etag_is_stable_across_identical_requests(client):
    await make(client, "a", priority="H")
    await make(client, "b", priority="H")          # equal urgency: ordering must
    await make(client, "c", priority="H")          # still be deterministic
    tags = {(await client.get("/api/tasks")).headers["etag"] for _ in range(4)}
    assert len(tags) == 1


async def test_etag_matches_weak_and_list_forms(client):
    await make(client, "one")
    etag = (await client.get("/api/tasks")).headers["etag"]

    for header in ("W/%s" % etag, '"nope", %s' % etag, "*"):
        r = await client.get("/api/tasks", headers={"If-None-Match": header})
        assert r.status_code == 304, header


async def test_etag_is_per_status_view(client):
    await make(client, "one")
    pending = (await client.get("/api/tasks")).headers["etag"]
    completed = (await client.get("/api/tasks",
                                  params={"status": "completed"})).headers["etag"]
    assert pending != completed


async def test_cors_headers_expose_the_etag(client):
    r = await client.get("/api/tasks", headers={"Origin": "capacitor://localhost"})
    assert r.headers["access-control-allow-origin"] == "*"
    assert "etag" in r.headers["access-control-expose-headers"].lower()
