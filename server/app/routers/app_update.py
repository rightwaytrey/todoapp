"""/api/app — over-the-air web bundle updates (docs/design.md D11, docs/api.md).

The shell embeds @capgo/capacitor-updater, self-hosted: at every cold launch it
POSTs here asking whether a newer `www/` exists, and if one does it downloads the
zip from `/api/app/bundles/…`, verifies its SHA-256 and swaps it in. That is what
turns a one-line change to `www/index.html` from ~30 billed macOS minutes into a
`scripts/publish_bundle.py` run.

**The contract is the plugin's, not ours, and it is asymmetric.** An offer is
`{version, url, checksum}` and *nothing else* — the plugin reads a `message` key
as "there is nothing to do". A non-offer is `{message, kind}` and MUST carry the
`kind`: `backgroundDownload()` routes a response to its no-update handler only
when `error` or `kind` is non-empty (CapacitorUpdaterPlugin.swift:4290 in
8.51.15), so a bare `{"message": "up to date"}` falls through to
`URL(string: res.url)` with an empty url, logs "Error no url or wrong format" and
counts a failed download — once per launch, on a phone that is perfectly healthy.
The three kinds it recognises are `up_to_date`, `blocked` and `failed`; anything
else normalises to `failed` (`normalizedUpdateResponseKind`, :4106).

Everything here answers **200**. A failed update check must never look like a
broken app: the phone keeps the bundle it has and tries again next launch.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..config import settings
from ..errors import not_found

log = logging.getLogger("taskmaster.app")

router = APIRouter(prefix="/app", tags=["app"])

# A version string reaches the filesystem as `www-<version>.zip`, so it is held
# to what a version may plausibly contain and nothing else: no separators, no
# dot-dot, no leading punctuation. `publish_bundle.py` writes `YYYY.MMDD.n`.
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}$")
CHECKSUM_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# The only filename shape /api/app/bundles/{file} will serve. The version group
# is VERSION_RE, so `/`, `\` and `..` cannot appear anywhere in it.
BUNDLE_RE = re.compile(r"^www-([0-9A-Za-z][0-9A-Za-z.+_-]{0,31})\.zip$")

# The `kind` values the plugin recognises. Sending anything else — or nothing —
# is the bug described in the module docstring.
KIND_UP_TO_DATE = "up_to_date"      # nothing to do; logged at info, no stats
KIND_BLOCKED = "blocked"            # deliberately withheld (min_native)
KIND_FAILED = "failed"              # something is wrong on this side


# --------------------------------------------------------------------------- #
# the manifest on disk
# --------------------------------------------------------------------------- #
def read_manifest() -> Optional[Dict[str, Any]]:
    """The currently published bundle, or None.

    Never raises. An absent, unreadable or malformed manifest has to mean "no
    bundle" rather than an error: the bundles directory is *empty by design*
    until the first store build carrying the plugin has shipped, and one bad
    file on this box must not turn every launch of the app into a failed check.

    `version` and `checksum` are validated here rather than trusted from the
    publish step, because the file can be hand-edited and the phone is what pays
    for a mistake — a wrong checksum costs it a download and a rollback.
    """
    try:
        data = json.loads((settings.bundles_dir / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    checksum = data.get("checksum")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        log.warning("manifest.json has an unusable version %r — ignoring it", version)
        return None
    if not isinstance(checksum, str) or not CHECKSUM_RE.match(checksum):
        log.warning("manifest.json has an unusable checksum for %s — ignoring it",
                    version)
        return None
    return data


def bundle_file(version: str) -> Optional[Path]:
    """The zip for a version, or None if the name is unsafe or the file is gone."""
    if not isinstance(version, str) or not VERSION_RE.match(version):
        return None
    return safe_path("www-%s.zip" % version)


def safe_path(name: str) -> Optional[Path]:
    """`<bundles_dir>/<name>` if `name` is a plain bundle filename, else None.

    Two locks on the same door. The regex already excludes every separator, so
    no traversal can be spelled; the resolved-parent check is what catches a
    symlink inside the bundles directory pointing out of it, and costs nothing.
    """
    if not isinstance(name, str) or not BUNDLE_RE.match(name):
        return None
    try:
        root = settings.bundles_dir.resolve()
        path = (root / name).resolve()
    except OSError:
        return None
    if path.parent != root:
        return None
    return path if path.is_file() else None


def version_tuple(value: Any) -> Tuple[int, ...]:
    """Leading numeric parts of a version, for ordering: '1.0.12' -> (1, 0, 12).

    Stops at the first non-numeric chunk, so '1.0.12-rc1' orders as (1, 0, 12).
    An unparseable or missing version yields `()`, which sorts below everything
    — a shell whose build we cannot read is treated as too old, never as new
    enough. Withholding a bundle from a phone that could have run it costs one
    launch; sending one to a phone that cannot costs a web layer calling a
    bridge that isn't there.
    """
    parts = []
    for chunk in re.split(r"[.\-+_]", str(value or "")):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


# --------------------------------------------------------------------------- #
# the plugin's check
# --------------------------------------------------------------------------- #
@router.post("/update")
async def app_update(request: Request):
    """Tell the shell whether a newer web bundle exists.

    Request body (@capgo/capacitor-updater 8.51.15 `InfoObject.toParameters()`,
    CapgoUpdater.swift:1018 — every field optional, and we read four of them):

        app_id        "org.rightwaytrey.taskmaster"  — checked
        version_name  the bundle it is running       — checked
        version_build the NATIVE marketing version   — checked against min_native
        device_id     a per-install uuid             — logged only
        platform, version_code, version_os, plugin_version, is_emulator,
        is_prod, install_source, custom_id, channel, defaultChannel, key_id
                                                     — sent, ignored here

    `version_name` is the *bundle* version (`2026.0904.1`), except on a shell
    that has never taken an update, where `getBundleInfo(id:)` reports the
    native marketing version instead (CapgoUpdater.swift:3397) — so a fresh
    install says `"1.0.7"`. That is why bundle versions are date-shaped: the two
    namespaces share one field and must never collide.

    `version_build` is `CFBundleShortVersionString` = `MARKETING_VERSION` =
    `1.0.<run number>` (CapacitorUpdaterPlugin.swift:268, and the workflow's
    export step). It is the number the Settings screen shows as **Build**, and
    it is what `min_native` gates on.

    The body is read leniently — no pydantic model. A 422 here would be an
    update check that looks like a broken app, and the contract this half is
    built to is the plugin's, which promises nothing about its request.
    """
    try:
        body = await request.json()
    except Exception:                                            # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        body = {}

    app_id = body.get("app_id")
    running = body.get("version_name")
    native = body.get("version_build")
    device = str(body.get("device_id") or "")[:64]

    manifest = read_manifest()
    offer = None
    message = kind = None

    if settings.bundle_app_id and app_id and app_id != settings.bundle_app_id:
        message, kind = "unknown app", KIND_FAILED
    elif manifest is None:
        # NOT "failed", though it is the shape transitnav uses. Here an empty
        # bundles directory is the normal, intended resting state — nothing is
        # published until the planner says so — and `failed` would fire the
        # plugin's downloadFailed listener and log an error on every launch of a
        # healthy app. "There is nothing newer than what you are running" is
        # both true and the thing the plugin logs quietly.
        message, kind = "no bundle published", KIND_UP_TO_DATE
    elif not settings.bundle_public_base:
        # There is a bundle, but no absolute base to build its download URL out
        # of (config.py: TASKMASTER_BUNDLE_BASE / TASKMASTER_PUBLIC_BASE, which
        # deliberately have no default in code). A relative url would resolve
        # against capacitor://localhost on the phone, so there is nothing
        # honest to offer. Same quiet answer as an empty store — but warn,
        # because unlike an empty store this is a misconfiguration, and the
        # journal is where it will be noticed.
        log.warning("a bundle is published but TASKMASTER_BUNDLE_BASE is unset, "
                    "so there is no absolute URL to offer. Set it in "
                    "~/.config/taskmaster/env and restart the unit; until then "
                    "every phone is told there is nothing to install.")
        message, kind = "no bundle published", KIND_UP_TO_DATE
    elif manifest["version"] == running:
        message, kind = "up to date", KIND_UP_TO_DATE
    elif manifest.get("min_native") and \
            version_tuple(native) < version_tuple(manifest["min_native"]):
        # A bundle may depend on something native — a plugin, an Info.plist key,
        # a widget. Handing it to an older shell would give the user a client
        # calling a bridge that isn't there, which is worse than no update.
        message = "needs native %s" % manifest["min_native"]
        kind = KIND_BLOCKED
    elif bundle_file(manifest["version"]) is None:
        # The manifest says one thing and the disk says another. Say nothing
        # rather than send the phone to a URL that 404s, which it would report
        # as a checksum failure against a bundle that is fine.
        message, kind = "bundle file missing", KIND_FAILED
    else:
        offer = {
            "version": manifest["version"],
            "url": "%s/api/app/bundles/www-%s.zip" % (
                settings.bundle_public_base, manifest["version"]),
            "checksum": manifest["checksum"],
        }

    # One line per app launch, in the journal rather than a file: it is the only
    # way to know a rollout reached a phone short of asking the user, and
    # `journalctl --user -u taskmaster-api | grep app-update` is enough for one
    # device. Nothing here is written to disk, so a check cannot fill it.
    log.info("app-update check: device=%s running=%s native=%s -> %s",
             device or "?", running, native,
             "offer %s" % offer["version"] if offer else "%s (%s)" % (message, kind))

    return offer if offer else {"message": message, "kind": kind}


@router.get("/update")
async def app_update_status():
    """What is published right now — for `scripts/ship_web.sh` and for a human.

    Deliberately a different shape from the POST, and deliberately not something
    the plugin ever calls: it must be impossible for this to be mistaken for an
    update offer.
    """
    manifest = read_manifest()
    if manifest is None:
        return {"published": False, "bundles_dir": str(settings.bundles_dir)}
    path = bundle_file(manifest["version"])
    return {
        "published": True,
        "version": manifest["version"],
        "checksum": manifest["checksum"],
        "min_native": manifest.get("min_native"),
        "published_at": manifest.get("published"),
        "notes": manifest.get("notes"),
        # None, not a URL missing its host: this view is what ship_web.sh
        # checks the offer against, and a bare path would look like a working
        # answer.
        "url": ("%s/api/app/bundles/www-%s.zip"
                % (settings.bundle_public_base, manifest["version"])
                if settings.bundle_public_base else None),
        "bytes": path.stat().st_size if path else None,
        "available": path is not None,
        "bundles_dir": str(settings.bundles_dir),
    }


@router.get("/bundles/{file}")
async def app_bundle(file: str):
    """Serve a published bundle zip.

    Only `www-<version>.zip` is served, and only out of the bundles directory —
    see `safe_path`. Anything else is a 404 in the docs/api.md envelope, the
    same answer a genuinely absent bundle gets, because telling a caller which
    of "malformed" and "missing" it hit tells it nothing it needs.

    A given version's bytes never change — publishing writes a new version
    rather than rewriting one — so the phone may cache it hard.
    """
    path = safe_path(file)
    if path is None:
        raise not_found("No such bundle.")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
