"""Two users, a shared store (docs/api.md round 9, docs/design.md D19).

Every test here except the single-user ones runs under the `two_users`
fixture: `trey` is the default user and owns the conftest throwaway store
(the ambient TASKRC/TASKDATA), `partner` and `shared` are stores under the
conftest STORES temp dir. All three are driven by the real `task` binary —
the move tests in particular are only worth anything against 3.4.2 itself.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime

import pytest

from app.config import parse_users, settings

from .conftest import (PARTNER_ADDR, PREFS, STORES, STRANGER_ADDR, TASK,
                       TREY_ADDR, client_from, store_task, task)


def trey():
    return client_from(TREY_ADDR)


def partner():
    return client_from(PARTNER_ADDR)


def uuids_in(store: str) -> set:
    """Every uuid in a store, any status. `default` is the conftest store."""
    res = task("export") if store == "default" else store_task(store, "export")
    return {t["uuid"] for t in json.loads(res.stdout.strip() or "[]")}


async def add(c, description, **fields):
    r = await c.post("/api/tasks", json=dict(description=description, **fields))
    assert r.status_code == 201, r.text
    return r.json()


async def uuids_listed(c, status="pending"):
    r = await c.get("/api/tasks", params={"status": status})
    assert r.status_code == 200, r.text
    return {t["uuid"] for t in r.json()}


async def share(c, name):
    return await c.post("/api/categories/share", json={"name": name})


def shared_file():
    return json.loads((STORES / "shared" / "categories.json").read_text())


def today():
    return datetime.now(settings.tz).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Single-user mode: nothing moves
# --------------------------------------------------------------------------- #
async def test_single_user_meta_user_is_null_and_nothing_is_shared(client):
    t = await add(client, "solo", project="work")
    assert t["shared"] is False
    body = (await client.get("/api/meta")).json()
    assert body["user"] is None
    assert all(c["shared"] is False for c in body["categories"])
    assert not STORES.exists()            # no stores are created at all


async def test_single_user_share_is_409_and_unshare_is_a_no_op(client):
    r = await share(client, "work")
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert "no users configured" in r.json()["detail"]
    r = await client.post("/api/categories/unshare", json={"name": "work"})
    assert r.status_code == 204
    assert r.headers["x-moved"] == "0"


async def test_single_user_any_tailnet_address_is_the_one_store(client):
    t = await add(client, "mine")
    async with client_from(PARTNER_ADDR) as c:
        assert t["uuid"] in await uuids_listed(c)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, why", [
    ("Trey=100.64.0.1", "must match"),
    ("shared=100.64.0.1", "reserved"),
    ("a=100.64.0.1;b=100.64.0.1", "mapped to both"),
    ("a=100.64.0.1;a=100.64.0.2", "twice"),
    ("a=not-an-ip", "not an IP"),
    ("a", "name=addr"),
])
def test_a_bad_user_map_refuses_to_start(raw, why):
    """A dropped entry would make her phone the default user — fail loudly."""
    with pytest.raises(ValueError, match=why):
        parse_users(raw)


def test_the_first_entry_is_the_default_user():
    specs = parse_users(" trey=100.64.1.2, 100.64.1.3 ; partner=100.64.1.9 ;")
    assert [(s.name, s.default) for s in specs] == [("trey", True),
                                                    ("partner", False)]
    assert [str(a) for a in specs[0].addrs] == ["100.64.1.2", "100.64.1.3"]


def test_store_taskrc_is_written_with_hooks_off(two_users):
    for name in ("partner", "shared"):
        rc = (STORES / name / "taskrc").read_text()
        assert "hooks=off" in rc
        assert "uda.order.type=numeric" in rc
        assert "data.location=%s" % (STORES / name / "data") in rc
    # The default user's store is the ambient one; nothing new is written for it.
    assert not (STORES / "trey").exists()


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host, who", [
    ("127.0.0.1", "trey"), ("::1", "trey"), (TREY_ADDR, "trey"),
    (PARTNER_ADDR, "partner"), ("::ffff:%s" % PARTNER_ADDR, "partner"),
    (STRANGER_ADDR, "trey"),
])
async def test_identity_is_the_client_address(two_users, host, who):
    async with client_from(host) as c:
        assert (await c.get("/api/meta")).json()["user"] == who


def test_a_mapped_loopback_address_is_that_user(monkeypatch):
    # Mapping 127.0.0.2 is an explicit act and is how the two-user path is
    # driven end to end on the box with `curl --interface` (stores.resolve).
    # Unmapped loopback stays the default user.
    from app import stores
    from app.config import parse_users
    monkeypatch.setattr(settings, "users",
                        parse_users("trey=%s;partner=%s,127.0.0.2"
                                    % (TREY_ADDR, PARTNER_ADDR)))
    assert stores.resolve("127.0.0.2").name == "partner"
    assert stores.resolve("127.0.0.1").name == "trey"
    assert stores.resolve("::1").name == "trey"


async def test_a_header_cannot_choose_the_user(two_users):
    async with trey() as c:
        r = await c.get("/api/meta", headers={"X-Forwarded-For": PARTNER_ADDR,
                                              "X-User": "partner"})
        assert r.json()["user"] == "trey"


# --------------------------------------------------------------------------- #
# Private isolation
# --------------------------------------------------------------------------- #
async def test_private_tasks_are_private_both_ways(two_users):
    async with trey() as t, partner() as p:
        mine = await add(t, "buy her a present", project="personal")
        hers = await add(p, "book the vet")
        assert mine["shared"] is False and hers["shared"] is False

        assert mine["uuid"] in uuids_in("default")
        assert hers["uuid"] in uuids_in("partner")
        assert hers["uuid"] not in uuids_in("default")

        assert await uuids_listed(t) == {mine["uuid"]}
        assert await uuids_listed(p) == {hers["uuid"]}


async def test_another_users_uuid_is_404_on_every_route(two_users):
    async with trey() as t, partner() as p:
        mine = await add(t, "secret")
        u = mine["uuid"]
        for method, path, body in (
                ("get", "/api/tasks/%s" % u, None),
                ("patch", "/api/tasks/%s" % u, {"description": "pwned"}),
                ("delete", "/api/tasks/%s" % u, None),
                ("post", "/api/tasks/%s/done" % u, None),
                ("post", "/api/tasks/%s/undone" % u, None),
                ("post", "/api/tasks/%s/annotations" % u, {"text": "hi"})):
            kw = {"json": body} if body is not None else {}
            r = await p.request(method.upper(), path, **kw)
            assert r.status_code == 404, (method, path, r.text)
            assert r.json()["error"] == "not_found"
        # And none of it touched the task.
        got = (await t.get("/api/tasks/%s" % u)).json()
        assert got["description"] == "secret" and got["status"] == "pending"


async def test_meta_never_leaks_the_other_users_categories_or_tags(two_users):
    async with trey() as t, partner() as p:
        await add(t, "x", project="surprise", tags=["gift"])
        meta = (await p.get("/api/meta")).json()
        assert "surprise" not in meta["projects"]
        assert "surprise" not in [c["name"] for c in meta["categories"]]
        assert "gift" not in meta["tags"]


async def test_the_other_users_fresh_prefs_do_not_carry_the_pa_five(two_users):
    # Her untouched document starts empty (api.md round 9 "Prefs are per
    # user"); the default user's still carries the five (design.md D13).
    async with partner() as c:
        p = (await c.get("/api/prefs")).json()
        assert p["categories"]["order"] == [] and p["chips"]["order"] == []
        assert [x["name"] for x in (await c.get("/api/meta")).json()["categories"]] == []
    async with trey() as c:
        assert (await c.get("/api/prefs")).json()["categories"]["order"][:5] == \
            ["personal", "work", "claude", "fun", "inbox"]


async def test_prefs_are_per_user(two_users):
    async with trey() as t, partner() as p:
        r = await p.put("/api/prefs", json={"sort": {"mode": "manual"}})
        assert r.status_code == 200
        assert (await p.get("/api/prefs")).json()["sort"]["mode"] == "manual"
        assert (await t.get("/api/prefs")).json()["sort"]["mode"] == "due"
        assert (STORES / "partner" / "prefs.json").exists()
        assert not PREFS.exists()

        r = await p.post("/api/widget/category", json={"category": "groceries"})
        assert r.status_code == 204
        assert (await p.get("/api/prefs")).json()["widget"]["category"] == "groceries"
        assert (await t.get("/api/prefs")).json()["widget"]["category"] is None


# --------------------------------------------------------------------------- #
# Create routes by category; the merged list
# --------------------------------------------------------------------------- #
async def test_create_in_a_shared_category_goes_to_the_shared_store(two_users):
    async with trey() as t, partner() as p:
        r = await share(t, "groceries")
        assert r.status_code == 204 and r.headers["x-moved"] == "0"
        assert shared_file() == {"categories": ["groceries"]}

        milk = await add(p, "milk", project="groceries")
        assert milk["shared"] is True
        assert milk["uuid"] in uuids_in("shared")
        assert milk["uuid"] not in uuids_in("partner")

        # Both see it, both can act on it.
        assert milk["uuid"] in await uuids_listed(t)
        r = await t.post("/api/tasks/%s/done" % milk["uuid"])
        assert r.status_code == 200 and r.json()["shared"] is True
        assert milk["uuid"] in await uuids_listed(p, "completed")


async def test_no_category_is_private(two_users):
    async with partner() as p:
        t = await add(p, "nothing")
        assert t["shared"] is False and t["uuid"] in uuids_in("partner")


async def test_merged_list_is_in_canonical_order_across_both_stores(two_users):
    async with trey() as t:
        await share(t, "groceries")
        a = await add(t, "a", due="2026-01-03")
        b = await add(t, "b", due="2026-01-02", project="groceries")
        c = await add(t, "c", due="2026-01-01")
        listed = [x["uuid"] for x in (await t.get("/api/tasks")).json()]
        assert listed == [c["uuid"], b["uuid"], a["uuid"]]


async def test_etag_changes_when_only_the_shared_store_changes(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        await add(t, "mine")
        first = (await t.get("/api/tasks")).headers["etag"]
        r = await t.get("/api/tasks", headers={"If-None-Match": first})
        assert r.status_code == 304

        await add(p, "eggs", project="groceries")       # her write, shared store
        r = await t.get("/api/tasks", headers={"If-None-Match": first})
        assert r.status_code == 200
        assert r.headers["etag"] != first

        await add(p, "her own")                          # her private store
        second = r.headers["etag"]
        r = await t.get("/api/tasks", headers={"If-None-Match": second})
        assert r.status_code == 304


async def test_widget_merges_under_the_callers_prefs(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        mine = await add(t, "mine today", due=today())
        hers = await add(p, "hers today", due=today())
        both = await add(p, "eggs today", due=today(), project="groceries")

        rows = {r["uuid"] for r in (await t.get("/api/widget")).json()["rows"]}
        assert rows == {mine["uuid"], both["uuid"]}
        rows = {r["uuid"] for r in (await p.get("/api/widget")).json()["rows"]}
        assert rows == {hers["uuid"], both["uuid"]}

        await p.post("/api/widget/category", json={"category": "groceries"})
        body = (await p.get("/api/widget")).json()
        assert [r["uuid"] for r in body["rows"]] == [both["uuid"]]
        assert (await t.get("/api/widget")).json()["category"] is None


# --------------------------------------------------------------------------- #
# Moving across the boundary
# --------------------------------------------------------------------------- #
async def test_patch_project_moves_the_task_and_keeps_everything(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        x = await add(t, "flour", project="personal", tags=["baking"],
                      priority="H", due="2026-11-01")
        u = x["uuid"]
        await t.post("/api/tasks/%s/annotations" % u, json={"text": "the good one"})
        await t.patch("/api/tasks/%s" % u, json={"order": 1500})

        r = await t.patch("/api/tasks/%s" % u, json={"project": "groceries",
                                                     "description": "bread flour"})
        assert r.status_code == 200, r.text
        moved = r.json()
        assert moved["uuid"] == u
        assert moved["shared"] is True
        assert moved["project"] == "groceries"
        assert moved["description"] == "bread flour"
        assert moved["tags"] == ["baking"] and moved["priority"] == "H"
        assert moved["due"] == "2026-11-01" and moved["order"] == 1500
        assert [a["text"] for a in moved["annotations"]] == ["the good one"]

        assert u in uuids_in("shared")
        assert u not in uuids_in("default")         # purged, not just deleted
        assert u in await uuids_listed(p)           # she sees it now

        # And back: shared -> private, to whoever moved it.
        r = await p.patch("/api/tasks/%s" % u, json={"project": None})
        assert r.status_code == 200, r.text
        assert r.json()["shared"] is False and r.json()["project"] is None
        assert u in uuids_in("partner")
        assert u not in uuids_in("shared")
        assert u not in await uuids_listed(t)


async def test_a_recurring_task_does_not_cross(two_users):
    async with trey() as t:
        await share(t, "groceries")
        x = await add(t, "water plants", due="2026-11-01")
        inst = (await t.patch("/api/tasks/%s" % x["uuid"],
                              json={"recur": "weekly"})).json()
        assert inst["parent"]
        r = await t.patch("/api/tasks/%s" % inst["uuid"],
                          json={"project": "groceries"})
        assert r.status_code == 422
        assert r.json()["error"] == "invalid_request"
        assert r.json()["detail"].startswith("project:")
        assert inst["uuid"] in uuids_in("default")
        assert uuids_in("shared") == set()


async def test_a_duplicate_resolves_to_the_shared_copy(two_users):
    """A move that died between import and purge leaves the uuid in both."""
    async with trey() as t:
        await share(t, "groceries")
        x = await add(t, "dup", project="groceries")
        # Copy it into the default (conftest, throwaway) store by hand.
        exported = store_task("shared", x["uuid"], "export").stdout
        subprocess.run([TASK, "rc.verbose=nothing", "rc.hooks=off", "import", "-"],
                       input=exported, text=True, check=True,
                       capture_output=True)
        assert x["uuid"] in uuids_in("default")

        # Listed once, as the shared copy...
        rows = [r for r in (await t.get("/api/tasks")).json()
                if r["uuid"] == x["uuid"]]
        assert len(rows) == 1 and rows[0]["shared"] is True
        # ...and the first lookup purges the private copy.
        r = await t.get("/api/tasks/%s" % x["uuid"])
        assert r.json()["shared"] is True
        assert x["uuid"] not in uuids_in("default")


# --------------------------------------------------------------------------- #
# Share / unshare
# --------------------------------------------------------------------------- #
async def test_share_moves_the_callers_tasks_and_leaves_recurring(two_users):
    async with trey() as t, partner() as p:
        a = await add(t, "a", project="house")
        b = await add(t, "b", project="house")
        await t.post("/api/tasks/%s/done" % b["uuid"])
        rep = await add(t, "bins", project="house", due="2026-11-01")
        inst = (await t.patch("/api/tasks/%s" % rep["uuid"],
                              json={"recur": "weekly"})).json()

        r = await share(t, "house")
        assert r.status_code == 204, r.text
        assert r.headers["x-moved"] == "2"
        assert r.headers["x-left"] == "2"          # the template and its instance
        assert shared_file() == {"categories": ["house"]}
        assert {a["uuid"], b["uuid"]} <= uuids_in("shared")
        assert inst["uuid"] in uuids_in("default")

        listed = await uuids_listed(p, "all")
        assert {a["uuid"], b["uuid"]} <= listed

        r = await share(t, "house")                 # already shared
        assert r.status_code == 204 and r.headers["x-moved"] == "0"


async def test_share_is_409_when_the_other_user_has_that_category(two_users):
    async with trey() as t, partner() as p:
        await add(p, "her work", project="work")
        mine = await add(t, "my work", project="work")
        r = await share(t, "work")
        assert r.status_code == 409
        assert r.json()["error"] == "conflict"
        assert "partner" in r.json()["detail"]
        assert mine["uuid"] in uuids_in("default")    # nothing moved
        assert not (STORES / "shared" / "categories.json").exists()


async def test_unshare_moves_every_task_to_the_caller(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        m = await add(t, "milk", project="groceries")
        e = await add(p, "eggs", project="groceries")
        await t.put("/api/prefs", json={
            "categories": {"order": ["groceries", "work"], "hidden": []},
            "chips": {"order": ["p:groceries", "t:claude"], "hidden": []},
            "widget": {"category": "groceries"}})

        r = await p.post("/api/categories/unshare", json={"name": "groceries"})
        assert r.status_code == 204
        assert r.headers["x-moved"] == "2"
        assert shared_file() == {"categories": []}
        assert {m["uuid"], e["uuid"]} <= uuids_in("partner")
        assert uuids_in("shared") == set()
        assert await uuids_listed(t) == set()

        prefs = (await t.get("/api/prefs")).json()
        assert prefs["categories"]["order"] == ["work"]
        assert prefs["chips"]["order"] == ["t:claude"]
        assert prefs["widget"]["category"] is None

        r = await p.post("/api/categories/unshare", json={"name": "groceries"})
        assert r.status_code == 204 and r.headers["x-moved"] == "0"


async def test_x_moved_is_exposed_to_the_app(two_users):
    async with trey() as t:
        r = await t.post("/api/categories/share", json={"name": "groceries"},
                         headers={"Origin": "capacitor://localhost"})
        assert r.status_code == 204
        exposed = r.headers["access-control-expose-headers"].lower()
        assert "x-moved" in exposed and "etag" in exposed


# --------------------------------------------------------------------------- #
# Rename / delete of shared categories
# --------------------------------------------------------------------------- #
async def _arrange_both(t, p, name):
    for c in (t, p):
        await c.put("/api/prefs", json={
            "categories": {"order": [name, "work"], "hidden": [name]},
            "chips": {"order": ["p:%s" % name], "hidden": []},
            "widget": {"category": name}})


async def test_shared_rename_runs_in_the_shared_store_and_every_users_prefs(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        x = await add(p, "milk", project="groceries")
        await _arrange_both(t, p, "groceries")

        r = await p.post("/api/categories/rename",
                         json={"from": "groceries", "to": "shopping"})
        assert r.status_code == 204, r.text
        assert shared_file() == {"categories": ["shopping"]}
        assert (await t.get("/api/tasks/%s" % x["uuid"])).json()["project"] == "shopping"
        for c in (t, p):
            prefs = (await c.get("/api/prefs")).json()
            assert prefs["categories"]["order"] == ["shopping", "work"]
            assert prefs["categories"]["hidden"] == ["shopping"]
            assert prefs["chips"]["order"] == ["p:shopping"]
            assert prefs["widget"]["category"] == "shopping"


async def test_rename_cannot_cross_the_line(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        await add(t, "mine", project="errands")
        await add(p, "hers", project="vet")
        r = await t.post("/api/categories/rename",
                         json={"from": "errands", "to": "groceries"})
        assert r.status_code == 409 and r.json()["error"] == "conflict"
        r = await t.post("/api/categories/rename",
                         json={"from": "groceries", "to": "vet"})
        assert r.status_code == 409 and "partner" in r.json()["detail"]
        assert shared_file() == {"categories": ["groceries"]}


async def test_private_rename_touches_only_the_caller(two_users):
    async with trey() as t, partner() as p:
        mine = await add(t, "mine", project="work")
        hers = await add(p, "hers", project="work")
        r = await p.post("/api/categories/rename", json={"from": "work", "to": "job"})
        assert r.status_code == 204
        assert (await t.get("/api/tasks/%s" % mine["uuid"])).json()["project"] == "work"
        assert (await p.get("/api/tasks/%s" % hers["uuid"])).json()["project"] == "job"


async def test_shared_delete_updates_every_users_prefs(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        await share(t, "house")
        x = await add(t, "milk", project="groceries")
        await _arrange_both(t, p, "groceries")

        r = await p.post("/api/categories/delete",
                         json={"name": "groceries", "move_to": "work"})
        assert r.status_code == 422 and r.json()["detail"].startswith("move_to:")

        r = await p.post("/api/categories/delete",
                         json={"name": "groceries", "move_to": "house"})
        assert r.status_code == 204, r.text
        assert shared_file() == {"categories": ["house"]}
        got = (await t.get("/api/tasks/%s" % x["uuid"])).json()
        assert got["project"] == "house" and got["shared"] is True
        for c in (t, p):
            prefs = (await c.get("/api/prefs")).json()
            assert prefs["categories"]["order"] == ["work"]
            assert prefs["categories"]["hidden"] == []
            assert prefs["chips"]["order"] == []
            assert prefs["widget"]["category"] == "house"


async def test_private_delete_into_a_shared_name_is_409(two_users):
    async with trey() as t:
        await share(t, "groceries")
        await add(t, "x", project="errands")
        r = await t.post("/api/categories/delete",
                         json={"name": "errands", "move_to": "groceries"})
        assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
async def test_meta_marks_shared_categories_and_counts_both_stores(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        await share(t, "empty")
        await add(t, "a", project="groceries")
        await add(p, "b", project="groceries")
        await add(t, "c", project="work", tags=["mine"])
        await add(p, "d", project="vet", tags=["hers"])

        meta = (await t.get("/api/meta")).json()
        cats = {c["name"]: c for c in meta["categories"]}
        assert cats["groceries"]["shared"] is True
        assert cats["groceries"]["count"] == 2
        assert cats["empty"] == {"name": "empty", "count": 0, "hidden": False,
                                 "shared": True}
        assert cats["work"]["shared"] is False and cats["work"]["count"] == 1
        assert "vet" not in cats
        assert meta["projects"] == ["personal", "work", "claude", "fun", "inbox",
                                    "empty", "groceries"]
        assert meta["tags"] == ["mine"]

        meta = (await p.get("/api/meta")).json()
        assert meta["user"] == "partner"
        # Not offered the pa five: they are trey's pa vocabulary.
        assert meta["projects"] == ["vet", "empty", "groceries"]
        assert meta["tags"] == ["hers"]


async def test_health_counts_only_the_default_store(two_users):
    async with trey() as t, partner() as p:
        await share(t, "groceries")
        await add(t, "mine")
        await add(p, "hers")
        await add(p, "shared one", project="groceries")
        assert (await p.get("/health")).json()["pending"] == 1
