"""/api/meta categories, and the rename/delete bulk operations.

A category *is* Taskwarrior's `project` (docs/design.md D13). The two tests that
matter most here are `test_rename_does_not_touch_a_category_with_the_same
_prefix` and `test_rename_moves_more_tasks_than_the_bulk_prompt_allows`: both
are silent-wrong-answer bugs in the obvious implementation, and neither shows up
with two tasks and no near-miss names.
"""
from __future__ import annotations

from .conftest import listing, make, task


def projects_of() -> dict:
    """description -> project, straight from the CLI, every status included."""
    import json
    out = task("export").stdout.strip()
    return {t["description"]: t.get("project") for t in json.loads(out or "[]")}


async def categories_of(client) -> list:
    return (await client.get("/api/meta")).json()["categories"]


# --------------------------------------------------------------------------- #
# GET /api/meta -> categories
# --------------------------------------------------------------------------- #
async def test_categories_are_in_prefs_order_with_counts(client):
    await make(client, "a", project="work")
    await make(client, "b", project="work")
    await make(client, "c", project="personal")

    got = await categories_of(client)
    assert [c["name"] for c in got] == ["personal", "work", "claude", "fun",
                                        "inbox"]
    counts = {c["name"]: c["count"] for c in got}
    assert counts == {"personal": 1, "work": 2, "claude": 0, "fun": 0,
                      "inbox": 0}
    assert all(c["hidden"] is False for c in got)


async def test_a_reordered_prefs_list_reorders_the_categories(client):
    await client.put("/api/prefs", json={
        "categories": {"order": ["work", "personal"], "hidden": ["personal"]}})
    got = await categories_of(client)
    assert [c["name"] for c in got] == ["work", "personal"]
    assert [c["hidden"] for c in got] == [False, True]


async def test_categories_in_use_but_not_in_prefs_come_after_alphabetically(client):
    await make(client, "a", project="zebra")
    await make(client, "b", project="garden")
    names = [c["name"] for c in await categories_of(client)]
    assert names[-2:] == ["garden", "zebra"]


async def test_a_category_only_a_completed_task_uses_still_appears(client):
    """It would be odd for a category to vanish the moment its last task is
    ticked off — the picker still has to offer it."""
    t = await make(client, "done thing", project="garden")
    await client.post("/api/tasks/%s/done" % t["uuid"])

    got = {c["name"]: c["count"] for c in await categories_of(client)}
    assert "garden" in got
    assert got["garden"] == 0                            # count is pending only


async def test_projects_is_unchanged_by_all_of_this(client):
    """Round-4 clients still read `projects`; it has no order of the user's."""
    await make(client, "a", project="garden")
    body = (await client.get("/api/meta")).json()
    assert body["projects"] == ["personal", "work", "claude", "fun", "inbox",
                                "garden"]


# --------------------------------------------------------------------------- #
# POST /api/categories/rename
# --------------------------------------------------------------------------- #
async def test_rename_moves_pending_and_completed_but_not_deleted(client):
    live = await make(client, "live", project="old")
    done = await make(client, "done", project="old")
    gone = await make(client, "gone", project="old")
    await client.post("/api/tasks/%s/done" % done["uuid"])
    await client.delete("/api/tasks/%s" % gone["uuid"])

    r = await client.post("/api/categories/rename",
                          json={"from": "old", "to": "new"})
    assert r.status_code == 204
    assert r.content == b""

    projects = projects_of()
    assert projects["live"] == "new"
    assert projects["done"] == "new"
    assert projects["gone"] == "old"                     # deleted is history
    assert (await client.get("/api/tasks/%s" % live["uuid"])).json()["project"] \
        == "new"


async def test_rename_moves_more_tasks_than_the_bulk_prompt_allows(client):
    """`rc.confirmation=off` does NOT cover Taskwarrior's bulk prompt: over
    three or more tasks the modify would ask, read EOF and change nothing while
    exiting 1. Five is comfortably past the default `bulk` of 2."""
    for i in range(5):
        await make(client, "task %d" % i, project="old")

    assert (await client.post("/api/categories/rename",
                              json={"from": "old", "to": "new"})).status_code == 204
    assert {t["project"] for t in await listing(client)} == {"new"}


async def test_rename_does_not_touch_a_category_with_the_same_prefix(client):
    """Taskwarrior attribute filters are PREFIX matches — `project:work` also
    selects `workshop` and `work.sub`. On the real database that is silent data
    loss, so every filter here is the `.is:` form."""
    await make(client, "exact", project="work")
    await make(client, "longer", project="workshop")
    await make(client, "child", project="work.sub")

    await client.post("/api/categories/rename", json={"from": "work", "to": "job"})

    projects = projects_of()
    assert projects["exact"] == "job"
    assert projects["longer"] == "workshop"
    assert projects["child"] == "work.sub"


async def test_rename_updates_the_prefs(client):
    await client.put("/api/prefs", json={
        "categories": {"order": ["work", "personal"], "hidden": ["work"]},
        "chips": {"order": ["p:work", "t:claude"], "hidden": ["p:personal"]},
        "widget": {"category": "work"},
    })
    await client.post("/api/categories/rename", json={"from": "work", "to": "job"})

    prefs = (await client.get("/api/prefs")).json()
    assert prefs["categories"]["order"] == ["job", "personal"]
    assert prefs["categories"]["hidden"] == ["job"]
    assert prefs["chips"]["order"] == ["p:job", "t:claude"]
    assert prefs["widget"]["category"] == "job"


async def test_rename_of_an_unused_category_still_succeeds(client):
    """With nothing to modify the CLI exits 1 with empty output; that is not an
    error, and the prefs rename is the whole point of the call."""
    r = await client.post("/api/categories/rename",
                          json={"from": "inbox", "to": "later"})
    assert r.status_code == 204
    assert "later" in (await client.get("/api/prefs")).json()["categories"]["order"]


async def test_rename_onto_an_existing_category_merges_it(client):
    await make(client, "a", project="work")
    await make(client, "b", project="personal")
    await client.post("/api/categories/rename",
                      json={"from": "work", "to": "personal"})

    assert {t["project"] for t in await listing(client)} == {"personal"}
    prefs = (await client.get("/api/prefs")).json()
    assert prefs["categories"]["order"].count("personal") == 1


async def test_rename_to_itself_is_a_no_op(client):
    await make(client, "a", project="work")
    assert (await client.post("/api/categories/rename",
                              json={"from": "work", "to": "work"})).status_code == 204
    assert projects_of()["a"] == "work"


async def test_rename_validates_both_names(client):
    for body, field in (({"from": "not a name", "to": "work"}, "from"),
                        ({"from": "work", "to": "not a name"}, "to"),
                        ({"from": "work", "to": ""}, "to"),
                        ({"to": "work"}, "from")):
        r = await client.post("/api/categories/rename", json=body)
        assert r.status_code == 422, body
        assert r.json()["error"] == "invalid_request"
        assert r.json()["detail"].startswith(field), (body, r.json())


# --------------------------------------------------------------------------- #
# POST /api/categories/delete
# --------------------------------------------------------------------------- #
async def test_delete_moves_the_tasks_somewhere_else(client):
    await make(client, "a", project="old")
    await make(client, "b", project="old")

    r = await client.post("/api/categories/delete",
                          json={"name": "old", "move_to": "inbox"})
    assert r.status_code == 204
    assert {t["project"] for t in await listing(client)} == {"inbox"}


async def test_delete_with_no_move_to_clears_the_category(client):
    await make(client, "a", project="old")
    done = await make(client, "b", project="old")
    await client.post("/api/tasks/%s/done" % done["uuid"])

    assert (await client.post("/api/categories/delete",
                              json={"name": "old", "move_to": None})).status_code \
        == 204
    assert projects_of() == {"a": None, "b": None}


async def test_delete_leaves_a_prefix_neighbour_alone(client):
    await make(client, "exact", project="work")
    await make(client, "longer", project="workshop")
    await client.post("/api/categories/delete", json={"name": "work"})
    assert projects_of() == {"exact": None, "longer": "workshop"}


async def test_delete_removes_it_from_the_prefs(client):
    await client.put("/api/prefs", json={
        "categories": {"order": ["work", "personal"], "hidden": ["work"]},
        "chips": {"order": ["p:work", "p:personal"], "hidden": ["p:work"]},
        "widget": {"category": "work"},
    })
    await client.post("/api/categories/delete",
                      json={"name": "work", "move_to": "personal"})

    prefs = (await client.get("/api/prefs")).json()
    assert prefs["categories"]["order"] == ["personal"]
    assert prefs["categories"]["hidden"] == []
    assert prefs["chips"]["order"] == ["p:personal"]
    assert prefs["chips"]["hidden"] == []
    # The widget followed the tasks rather than filtering to an empty screen.
    assert prefs["widget"]["category"] == "personal"


async def test_delete_without_a_move_to_clears_the_widget_filter(client):
    await client.put("/api/prefs", json={"widget": {"category": "work"}})
    await client.post("/api/categories/delete", json={"name": "work"})
    assert (await client.get("/api/prefs")).json()["widget"]["category"] is None


async def test_delete_into_itself_is_422(client):
    r = await client.post("/api/categories/delete",
                          json={"name": "work", "move_to": "work"})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("move_to:")


async def test_delete_validates_the_names(client):
    for body, field in (({"name": "not a name"}, "name"),
                        ({"name": "work", "move_to": "not a name"}, "move_to"),
                        ({}, "name")):
        r = await client.post("/api/categories/delete", json=body)
        assert r.status_code == 422, body
        assert r.json()["detail"].startswith(field), (body, r.json())


async def test_delete_of_an_unused_category_still_succeeds(client):
    assert (await client.post("/api/categories/delete",
                              json={"name": "fun"})).status_code == 204
    assert "fun" not in (await client.get("/api/prefs")).json()["categories"]["order"]
