"""/health and /api/meta."""
from __future__ import annotations

from .conftest import make


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "0.1.0"
    assert body["tz"] == "America/Chicago"
    assert body["task_version"].startswith("3.")
    assert body["pending"] == 0
    assert body["time"].endswith(("-05:00", "-06:00"))       # CDT / CST


async def test_health_counts_pending(client):
    await make(client, "one")
    await make(client, "two")
    assert (await client.get("/health")).json()["pending"] == 2


async def test_health_reports_a_broken_task_binary(client):
    from app.config import settings

    original = settings.task_bin
    settings.task_bin = "/nonexistent/task"
    try:
        r = await client.get("/health")
        assert r.status_code == 200                          # still 200
        body = r.json()
        assert body["ok"] is False
        assert body["task_version"] is None
        assert "nonexistent" in body["detail"]
    finally:
        settings.task_bin = original


async def test_meta_shape(client):
    await make(client, "a", project="work", tags=["claude"])
    await make(client, "b", project="garden")

    body = (await client.get("/api/meta")).json()
    # The five pa projects first, in order, then anything else alphabetically.
    assert body["projects"] == ["personal", "work", "claude", "fun", "inbox",
                                "garden"]
    assert body["tags"] == ["claude"]
    assert body["priorities"] == ["H", "M", "L"]
    assert body["tz"] == "America/Chicago"
    assert body["now"]


async def test_meta_hides_reserved_tags(client):
    """`pa retag` paints today/overdue/due on; the client must never see them."""
    from .conftest import task

    t = await make(client, "tagged", tags=["claude"])
    task(t["uuid"], "modify", "+today", "+overdue", "+due")

    assert (await client.get("/api/meta")).json()["tags"] == ["claude"]
