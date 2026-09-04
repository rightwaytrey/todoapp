"""/api/app/update and /api/app/bundles — the live-update contract (D11).

Every assertion here is about the *shape* of the answer, because the shape is
the contract and the plugin is unforgiving about it: an offer that carries a
`message` is ignored, and a non-offer that omits `kind` is read as a broken
download (see the module docstring of app/routers/app_update.py). Nothing about
Taskwarrior is involved, so these run against a throwaway bundles directory
rather than the real one — and the real one is meant to stay empty anyway.
"""
from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from app.config import reload_settings, settings

APP_ID = "org.rightwaytrey.taskmaster"
# Any absolute base does: the assertions are about the offer's SHAPE, and the
# real one is not in this repo (config.py, bundle_public_base — no default in
# code). Every test that needs it sets it explicitly through the fixture.
BASE = "http://taskmaster.example:8101"


@pytest.fixture
def bundles(tmp_path, monkeypatch):
    """An empty bundles directory, pointed at by the environment knob."""
    d = tmp_path / "bundles"
    d.mkdir()
    monkeypatch.setenv("TASKMASTER_BUNDLES_DIR", str(d))
    monkeypatch.setenv("TASKMASTER_BUNDLE_BASE", BASE)
    reload_settings()
    assert settings.bundles_dir == d
    yield d
    monkeypatch.delenv("TASKMASTER_BUNDLES_DIR", raising=False)
    # raising=False: a test may have unset it itself to exercise the
    # no-public-base path, and this teardown only has to leave the process
    # environment clean for the next one.
    monkeypatch.delenv("TASKMASTER_BUNDLE_BASE", raising=False)
    reload_settings()


def publish(d, version="2026.0904.1", body=b"<!doctype html>hi", **extra):
    """Write a zip + manifest by hand — the same files publish_bundle.py writes."""
    blob = d / ("www-%s.zip" % version)
    with zipfile.ZipFile(blob, "w") as zf:
        zf.writestr("index.html", body)
    manifest = {
        "version": version,
        "file": blob.name,
        "checksum": hashlib.sha256(blob.read_bytes()).hexdigest(),
        "published": "2026-09-04T12:00:00Z",
        "notes": "",
    }
    manifest.update(extra)
    (d / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def check(**over):
    """The body @capgo/capacitor-updater POSTs. Defaults to a fresh install."""
    body = {
        "platform": "ios",
        "device_id": "5B1F0C8E-0000-4000-8000-000000000001",
        "app_id": APP_ID,
        # A shell that has never taken a bundle reports its NATIVE marketing
        # version here, not "builtin" (CapgoUpdater.swift:3397).
        "version_name": "1.0.7",
        "version_build": "1.0.7",
        "version_code": "7",
        "version_os": "18.5",
        "plugin_version": "8.51.15",
        "is_emulator": False,
        "is_prod": True,
    }
    body.update(over)
    return body


# --------------------------------------------------------------------------- #
# no update
# --------------------------------------------------------------------------- #
async def test_no_manifest_is_a_quiet_no_update(client, bundles):
    """An empty bundles directory is the resting state, not an error."""
    r = await client.post("/api/app/update", json=check())
    assert r.status_code == 200
    assert r.json() == {"message": "no bundle published", "kind": "up_to_date"}


async def test_a_non_offer_always_carries_kind(client, bundles):
    """The single most expensive thing to get wrong.

    Without `kind`, backgroundDownload() falls through to `URL(string: res.url)`
    with an empty url and logs "Error no url or wrong format" once per launch on
    a healthy phone.
    """
    publish(bundles, min_native="9.9.9")
    for body in (check(), check(app_id="com.someone.else"),
                 check(version_name="2026.0904.1")):
        payload = (await client.post("/api/app/update", json=body)).json()
        if "version" in payload:
            continue
        assert payload.get("kind") in ("up_to_date", "blocked", "failed"), payload


async def test_running_the_published_version(client, bundles):
    publish(bundles, version="2026.0904.1")
    r = await client.post("/api/app/update",
                          json=check(version_name="2026.0904.1"))
    assert r.json() == {"message": "up to date", "kind": "up_to_date"}


async def test_no_public_base_is_a_quiet_no_update(client, bundles, monkeypatch):
    """An unset TASKMASTER_BUNDLE_BASE must not produce a relative download URL.

    There is no default in code (config.py) because the value is the user's own
    host name. With it unset there is a bundle on disk and no absolute URL to
    reach it by — a relative one would resolve against capacitor://localhost on
    the phone — so the server says "nothing published", the same quiet answer an
    empty store gives, and warns in the journal.
    """
    publish(bundles)
    monkeypatch.delenv("TASKMASTER_BUNDLE_BASE")
    reload_settings()
    assert settings.bundle_public_base == ""
    r = await client.post("/api/app/update", json=check())
    assert r.json() == {"message": "no bundle published", "kind": "up_to_date"}

    status = (await client.get("/api/app/update")).json()
    assert status["published"] is True
    assert status["url"] is None


async def test_unknown_app_id(client, bundles):
    publish(bundles)
    r = await client.post("/api/app/update", json=check(app_id="com.someone.else"))
    assert r.json() == {"message": "unknown app", "kind": "failed"}


async def test_manifest_naming_a_zip_that_is_not_there(client, bundles):
    publish(bundles, version="2026.0904.1")
    (bundles / "www-2026.0904.1.zip").unlink()
    r = await client.post("/api/app/update", json=check())
    assert r.json() == {"message": "bundle file missing", "kind": "failed"}


@pytest.mark.parametrize("text", ["", "{", "[]", '{"version": "../etc"}',
                                  '{"version": "2026.0904.1"}',
                                  '{"version": "2026.0904.1", "checksum": "nope"}'])
async def test_an_unusable_manifest_reads_as_no_bundle(client, bundles, text):
    """One bad file must not turn every launch into a failed check."""
    (bundles / "manifest.json").write_text(text)
    r = await client.post("/api/app/update", json=check())
    assert r.json() == {"message": "no bundle published", "kind": "up_to_date"}


async def test_a_junk_body_is_still_a_200(client, bundles):
    """A 422 here would be an update check that looks like a broken app."""
    for r in (await client.post("/api/app/update"),
              await client.post("/api/app/update", content=b"not json"),
              await client.post("/api/app/update", json=[1, 2, 3])):
        assert r.status_code == 200
        assert r.json()["kind"] == "up_to_date"


# --------------------------------------------------------------------------- #
# an update is available
# --------------------------------------------------------------------------- #
async def test_update_available(client, bundles):
    manifest = publish(bundles, version="2026.0904.2")
    r = await client.post("/api/app/update", json=check())
    assert r.status_code == 200
    # EXACTLY these three keys. A `message` alongside them is read by the plugin
    # as "there is nothing to do", so the offer would be silently dropped.
    assert r.json() == {
        "version": "2026.0904.2",
        "url": BASE + "/api/app/bundles/www-2026.0904.2.zip",
        "checksum": manifest["checksum"],
    }


async def test_the_offered_url_downloads_and_matches_its_checksum(client, bundles):
    """Exactly what the plugin does next, and the only end-to-end that matters."""
    manifest = publish(bundles, version="2026.0904.3")
    offer = (await client.post("/api/app/update", json=check())).json()

    r = await client.get(offer["url"].replace(BASE, ""))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "immutable" in r.headers["cache-control"]
    assert hashlib.sha256(r.content).hexdigest() == offer["checksum"] \
        == manifest["checksum"]


async def test_the_served_zip_has_index_html_at_its_root(client, bundles):
    import io
    publish(bundles, version="2026.0904.4")
    r = await client.get("/api/app/bundles/www-2026.0904.4.zip")
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == ["index.html"]


# --------------------------------------------------------------------------- #
# min_native
# --------------------------------------------------------------------------- #
async def test_min_native_withholds_from_an_older_shell(client, bundles):
    publish(bundles, version="2026.0904.5", min_native="1.0.9")
    r = await client.post("/api/app/update", json=check(version_build="1.0.7"))
    assert r.json() == {"message": "needs native 1.0.9", "kind": "blocked"}


async def test_min_native_lets_the_matching_shell_through(client, bundles):
    publish(bundles, version="2026.0904.5", min_native="1.0.9")
    for build in ("1.0.9", "1.0.10", "1.1.0", "2.0.0"):
        payload = (await client.post("/api/app/update",
                                     json=check(version_build=build))).json()
        assert payload.get("version") == "2026.0904.5", (build, payload)


@pytest.mark.parametrize("build", [None, "", "unknown", "beta"])
async def test_an_unreadable_native_version_is_treated_as_too_old(client, bundles,
                                                                  build):
    """The safe direction: withholding costs a launch, mis-sending costs a
    client calling a bridge that isn't there."""
    publish(bundles, version="2026.0904.5", min_native="1.0.9")
    r = await client.post("/api/app/update", json=check(version_build=build))
    assert r.json()["kind"] == "blocked"


async def test_no_min_native_means_every_shell(client, bundles):
    publish(bundles, version="2026.0904.6")
    r = await client.post("/api/app/update", json=check(version_build="0.0.1"))
    assert r.json()["version"] == "2026.0904.6"


# --------------------------------------------------------------------------- #
# the download endpoint on its own
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "manifest.json",                      # real, present, and not a bundle
    "www-2026.0904.9.zip",                # well-formed, simply not published
    "www-.zip", "www.zip", "index.html",
    "www-2026.0904.1.zip.bak",
    "WWW-2026.0904.1.ZIP",                # the match is case-sensitive
    "www-2026.0904.1.zip%00.png",
])
async def test_only_a_plain_bundle_name_is_served(client, bundles, name):
    publish(bundles, version="2026.0904.1")
    r = await client.get("/api/app/bundles/" + name)
    assert r.status_code == 404, name
    assert r.json()["error"] == "not_found", name


@pytest.mark.parametrize("name", ["..", "../../etc/passwd",
                                  "www-../../etc/passwd.zip",
                                  "%2e%2e%2f%2e%2e%2fetc%2fpasswd"])
async def test_traversal_never_reaches_the_route_at_all(client, bundles, name):
    """These normalise away before routing, so they 404 as an unknown path.

    That is a *different* 404 from the one above — FastAPI's own
    `{"detail": "Not Found"}`, not this API's `{"error": …}` envelope — because
    nothing in app/ ever sees them. Asserted so a future router that adds a
    `{file:path}` converter (which would stop the normalisation) fails here.
    """
    publish(bundles, version="2026.0904.1")
    r = await client.get("/api/app/bundles/" + name)
    assert r.status_code == 404, name
    assert b"PK" not in r.content[:2], name


async def test_a_symlink_out_of_the_bundles_dir_is_refused(client, bundles,
                                                           tmp_path):
    """The regex cannot spell traversal; this is what the second lock catches."""
    secret = tmp_path / "secret.zip"
    secret.write_bytes(b"PK\x03\x04not-yours")
    (bundles / "www-9999.9999.9.zip").symlink_to(secret)
    r = await client.get("/api/app/bundles/www-9999.9999.9.zip")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# the status endpoint (humans and ship_web.sh, never the plugin)
# --------------------------------------------------------------------------- #
async def test_status_when_nothing_is_published(client, bundles):
    body = (await client.get("/api/app/update")).json()
    assert body["published"] is False
    # No `version`, `url` or `checksum`: a GET must never be mistakable for an
    # offer if something ever points the plugin at it.
    assert not {"version", "url", "checksum"} & set(body)


async def test_status_when_something_is(client, bundles):
    manifest = publish(bundles, version="2026.0904.7", min_native="1.0.9")
    body = (await client.get("/api/app/update")).json()
    assert body["published"] is True
    assert body["version"] == "2026.0904.7"
    assert body["checksum"] == manifest["checksum"]
    assert body["min_native"] == "1.0.9"
    assert body["available"] is True
    assert body["bytes"] > 0
    assert body["url"].endswith("/api/app/bundles/www-2026.0904.7.zip")


# --------------------------------------------------------------------------- #
# access
# --------------------------------------------------------------------------- #
async def test_the_address_allowlist_still_applies(bundles):
    from .conftest import client_from

    publish(bundles)
    async with client_from("192.168.1.5") as c:
        assert (await c.post("/api/app/update", json=check())).status_code == 403
        r = await c.get("/api/app/bundles/www-2026.0904.1.zip")
        assert r.status_code == 403


async def test_the_token_gate_does_not_apply_here(client, bundles, monkeypatch):
    """Not a relaxation we chose: the plugin cannot send an Authorization header.

    createRequest (CapgoUpdater.swift:207) sets User-Agent, Accept and
    Content-Type, and there is no config key for headers — so a gated check
    would 401 on every launch and the symptom would be "live updates just don't
    work" with nothing naming the token.
    """
    publish(bundles, version="2026.0904.8")
    monkeypatch.setenv("TASKMASTER_TOKEN", "s3cret")
    reload_settings()
    try:
        assert (await client.get("/api/tasks")).status_code == 401

        r = await client.post("/api/app/update", json=check())
        assert r.status_code == 200
        assert r.json()["version"] == "2026.0904.8"
        assert (await client.get(
            "/api/app/bundles/www-2026.0904.8.zip")).status_code == 200
    finally:
        monkeypatch.delenv("TASKMASTER_TOKEN")
        reload_settings()
