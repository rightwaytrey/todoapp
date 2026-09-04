"""GET/PUT /api/prefs — the one settings document (docs/design.md D15)."""
from __future__ import annotations

import json

from app.config import settings

DEFAULTS = {
    "categories": {"order": ["personal", "work", "claude", "fun", "inbox"],
                   "hidden": []},
    "chips": {"order": ["p:personal", "p:work", "p:claude", "p:fun", "p:inbox",
                        "t:claude", "t:alert"],
              "hidden": []},
    "sort": {"mode": "due"},
    "widget": {"groups": ["overdue", "today"], "upcoming_days": 7,
               "category": None, "rows": {"small": 3, "medium": 5, "large": 12},
               "show_category": False},
}


async def test_defaults_before_anything_is_written(client):
    """No file on disk is the resting state of a fresh install, not an error."""
    assert not settings.prefs_path.exists()
    r = await client.get("/api/prefs")
    assert r.status_code == 200
    assert r.json() == DEFAULTS


async def test_put_replaces_and_round_trips(client):
    body = {
        "categories": {"order": ["work", "personal"], "hidden": ["inbox"]},
        "chips": {"order": ["p:work", "t:alert"], "hidden": ["p:fun"]},
        "sort": {"mode": "manual"},
        "widget": {"groups": ["overdue", "today", "upcoming"],
                   "upcoming_days": 3, "category": "work",
                   "rows": {"small": 2, "medium": 4, "large": 20},
                   "show_category": True},
    }
    r = await client.put("/api/prefs", json=body)
    assert r.status_code == 200
    assert r.json() == body
    assert (await client.get("/api/prefs")).json() == body


async def test_put_fills_every_missing_section_with_its_default(client):
    r = await client.put("/api/prefs", json={"sort": {"mode": "urgency"}})
    body = r.json()
    assert body["sort"] == {"mode": "urgency"}
    assert body["categories"] == DEFAULTS["categories"]
    assert body["chips"] == DEFAULTS["chips"]
    assert body["widget"] == DEFAULTS["widget"]


async def test_put_empty_object_is_the_defaults(client):
    assert (await client.put("/api/prefs", json={})).json() == DEFAULTS


async def test_unknown_keys_are_dropped_not_rejected(client):
    """The client and the widget are written against the same document; a
    stray key must not be an integration failure (docs/api.md Errors)."""
    r = await client.put("/api/prefs", json={
        "sort": {"mode": "due", "direction": "sideways"},
        "theme": "midnight",
    })
    assert r.status_code == 200
    body = r.json()
    assert body == DEFAULTS
    assert "theme" not in body and "direction" not in body["sort"]
    assert "theme" not in json.loads(settings.prefs_path.read_text())


async def test_the_file_is_written_where_the_setting_says(client):
    await client.put("/api/prefs", json={"sort": {"mode": "priority"}})
    stored = json.loads(settings.prefs_path.read_text())
    assert stored["sort"]["mode"] == "priority"
    # And nothing is left behind by the atomic write.
    assert not settings.prefs_path.with_suffix(".tmp").exists()


async def test_the_parent_directory_is_created(client):
    nested = settings.prefs_path.parent / "deeper" / "prefs.json"
    original = settings.prefs_path
    settings.prefs_path = nested
    try:
        assert (await client.put("/api/prefs", json={})).status_code == 200
        assert nested.is_file()
    finally:
        settings.prefs_path = original


# --------------------------------------------------------------------------- #
# Validation — 422 in the docs/api.md envelope, naming the field
# --------------------------------------------------------------------------- #
BAD = [
    ("sort", {"sort": {"mode": "alphabetical"}}),
    ("sort", {"sort": {"mode": 3}}),
    ("categories", {"categories": {"order": ["has space"]}}),
    ("categories", {"categories": {"order": "work"}}),
    ("categories", {"categories": {"hidden": [7]}}),
    ("chips", {"chips": {"order": ["work"]}}),          # no p:/t: prefix
    ("chips", {"chips": {"order": ["x:work"]}}),
    ("widget", {"widget": {"groups": ["yesterday"]}}),
    ("widget", {"widget": {"upcoming_days": -1}}),
    ("widget", {"widget": {"upcoming_days": 400}}),
    ("widget", {"widget": {"upcoming_days": "seven"}}),
    ("widget", {"widget": {"category": "not a name"}}),
    ("widget", {"widget": {"rows": {"large": 500}}}),
    ("widget", {"widget": {"show_category": "yes"}}),
]


async def test_invalid_values_are_422_naming_the_field(client):
    for field, body in BAD:
        r = await client.put("/api/prefs", json=body)
        assert r.status_code == 422, body
        payload = r.json()
        assert payload["error"] == "invalid_request", body
        assert payload["detail"].startswith(field), (body, payload)


async def test_a_rejected_put_leaves_the_stored_document_alone(client):
    await client.put("/api/prefs", json={"sort": {"mode": "manual"}})
    await client.put("/api/prefs", json={"sort": {"mode": "nonsense"}})
    assert (await client.get("/api/prefs")).json()["sort"]["mode"] == "manual"


async def test_names_are_de_duplicated_in_order(client):
    r = await client.put("/api/prefs", json={
        "categories": {"order": ["work", "personal", "work"]}})
    assert r.json()["categories"]["order"] == ["work", "personal"]


# --------------------------------------------------------------------------- #
# A file the user (or an older version) wrote by hand
# --------------------------------------------------------------------------- #
async def test_a_corrupt_file_reads_as_the_defaults(client):
    """One bad byte here must not take the task list down with it."""
    settings.prefs_path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", "[]", '{"sort": {"mode": "made-up"}}', ""):
        settings.prefs_path.write_text(junk)
        assert (await client.get("/api/prefs")).json() == DEFAULTS, junk
        assert (await client.get("/api/tasks")).status_code == 200, junk


async def test_a_hand_edited_file_is_read_back(client):
    settings.prefs_path.parent.mkdir(parents=True, exist_ok=True)
    settings.prefs_path.write_text(json.dumps({"sort": {"mode": "urgency"}}))
    body = (await client.get("/api/prefs")).json()
    assert body["sort"]["mode"] == "urgency"
    assert body["widget"] == DEFAULTS["widget"]          # the rest defaults
