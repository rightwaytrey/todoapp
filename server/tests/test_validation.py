"""422s, and that the detail names the field (docs/api.md Errors)."""
from __future__ import annotations

import pytest

from .conftest import make


async def bad(client, body, field):
    r = await client.post("/api/tasks", json=body)
    assert r.status_code == 422, r.text
    out = r.json()
    assert out["error"] == "invalid_request"
    # "tags:" from our own validators, "tags.0:" when pydantic's own type check
    # fires first — either way the field is named, which is the contract.
    assert out["detail"].startswith(field), out["detail"]
    return out


async def test_description_is_required(client):
    await bad(client, {}, "description")
    await bad(client, {"description": "   "}, "description")
    await bad(client, {"description": 5}, "description")


@pytest.mark.parametrize("project", ["has space", "a" * 41, "semi;colon", "sl/ash"])
async def test_bad_project(client, project):
    await bad(client, {"description": "x", "project": project}, "project")


@pytest.mark.parametrize("priority", ["X", "high", "1"])
async def test_bad_priority(client, priority):
    await bad(client, {"description": "x", "priority": priority}, "priority")


async def test_priority_is_case_insensitive(client):
    t = await make(client, "x", priority="h")
    assert t["priority"] == "H"


@pytest.mark.parametrize("due", [
    "tomorrow",                 # a Taskwarrior relative date must not slip through
    "2026-9-5",
    "2026-09-05 14:30",
    "2026-09-05T14:30:00",      # seconds are not one of the two shapes
    "2026-02-30",               # not a real day
    "2026-09-05T25:00",
    "",
])
async def test_bad_due(client, due):
    await bad(client, {"description": "x", "due": due}, "due")


@pytest.mark.parametrize("tags", [["has space"], ["dash-not-allowed"], ["a" * 41],
                                  [""], [5], "claude"])
async def test_bad_tags(client, tags):
    await bad(client, {"description": "x", "tags": tags}, "tags")


async def test_patch_rejects_a_null_description(client):
    t = await make(client, "x")
    r = await client.patch("/api/tasks/%s" % t["uuid"],
                           json={"description": None})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("description:")


async def test_patch_rejects_null_tags(client):
    t = await make(client, "x")
    r = await client.patch("/api/tasks/%s" % t["uuid"], json={"tags": None})
    assert r.status_code == 422
    assert "send []" in r.json()["detail"]


async def test_unknown_keys_are_ignored_not_rejected(client):
    r = await client.post("/api/tasks", json={"description": "x",
                                              "uuid": "nope", "urgency": 99})
    assert r.status_code == 201
    assert r.json()["description"] == "x"


async def test_error_envelope_on_a_404(client):
    r = await client.get("/api/tasks/00000000-0000-0000-0000-000000000000")
    assert set(r.json()) == {"error", "detail"}


async def test_task_failed_is_502_with_the_cli_text(client, monkeypatch):
    """The 502 path: make `task` itself fail and check the envelope."""
    from app.config import settings

    t = await make(client, "x")
    monkeypatch.setattr(settings, "task_bin", "/bin/false")
    r = await client.post("/api/tasks", json={"description": "y"})
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "task_failed"
    assert body["detail"]
    assert t["uuid"]
