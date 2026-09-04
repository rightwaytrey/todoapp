#!/usr/bin/env python3
"""Package www/ as a live-update bundle and publish it to the server's store.

    python3 scripts/publish_bundle.py --www www --notes "fix the Done tab"

Writes `<bundles-dir>/www-<version>.zip` and rewrites `<bundles-dir>/manifest.json`,
which is what the API's `POST /api/app/update` serves to the phone
(docs/design.md D11, docs/api.md "App bundle updates"). Phones pick it up at
their next cold launch — no store build, no macOS runner, no billed minutes.

**It never touches the repo's www/index.html.** The tree is copied to a
temporary directory and the `__BUNDLE__` and `__API_BASE__` placeholders are
stamped *there*, so the committed file is still the file with the placeholders
in it. That matters: CI stamps `__BUILD__` and `__API_BASE__` the same way and a
script that edited in place would leave the working tree dirty in a way that
looked like a real change.

`__API_BASE__` is the app's default server URL, and it is the one value here
that names the user's own host, so it is not in the repo at all (design.md D4).
It comes from `TASKMASTER_PUBLIC_BASE` in the environment, or from
`~/.config/taskmaster/env` — the same file the systemd unit reads, outside the
repo, mode 600. Publishing without it is refused: a bundle stamped with the
literal placeholder would replace a phone's working client with one that has no
server, and the phone would have no way back but Settings.

The checks are not ceremony. A bundle that cannot boot far enough to call
`notifyAppReady()` is reverted by the plugin at the next launch, so publishing
one costs the user two relaunches and tells them nothing about why. Cheaper to
refuse here, where the message can say what is wrong.

What this CANNOT ship: anything native — a Capacitor plugin, an Info.plist key,
the widget, the updater's own config. Those still need a TestFlight build. Gate
a bundle that depends on one with `--min-native`, and the API withholds it from
every older shell (`"needs native 1.0.9"`).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLES = REPO / "server" / "bundles"

# The literal the client carries so it can show which bundle it is running.
# Stamped in the COPY, never in www/index.html itself.
PLACEHOLDER = "__BUNDLE__"
# The app's default server URL, stamped in the COPY from the environment. CI
# stamps the same literal from the TASKMASTER_API_BASE repository secret; the
# two must be the same value, or a phone that takes this bundle loses the
# default it was shipped with.
API_PLACEHOLDER = "__API_BASE__"
# Not in the repo, and not in any file the repo tracks.
ENV_FILE = Path.home() / ".config" / "taskmaster" / "env"
PUBLIC_BASE_VARS = ("TASKMASTER_PUBLIC_BASE", "TASKMASTER_BUNDLE_BASE")
# The rollback handshake. Without this call the plugin reverts every bundle at
# the next launch — silently — and the symptom is "updates just don't work".
HANDSHAKE = "notifyAppReady"
# Same shape the server's VERSION_RE accepts, because the version becomes a
# filename: www-<version>.zip.
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}$")
# Date-based, so a bundle version can never be mistaken for a native build
# number. They share one field on the wire: a shell that has never taken an
# update reports its native `1.0.<run>` as `version_name` (see the server's
# app_update.py), so `2026.0904.1` vs `1.0.7` must stay unambiguous.
DAY_FMT = "%Y.%m%d"


def die(msg: str) -> "None":
    sys.exit("error: " + msg)


def _from_env_file(name: str) -> str:
    """Read one KEY=value out of ~/.config/taskmaster/env.

    A deliberately small reader, not a dotenv library: the file is a systemd
    EnvironmentFile (deploy/taskmaster-api.service), which is exactly this
    format — no export, no interpolation, no multi-line values.
    """
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def public_base() -> str:
    """The API base stamped into the bundle's copy of index.html.

    Never committed — it names the user's own server. The environment wins; the
    env file is the fallback so `publish_bundle.py` works from a plain shell the
    way it does from `ship_web.sh` and from the systemd unit's environment.
    """
    for name in PUBLIC_BASE_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            break
    else:
        for name in PUBLIC_BASE_VARS:
            value = _from_env_file(name)
            if value:
                break
    value = (value or "").strip().rstrip("/")
    if not value or value.startswith("__"):
        die("%s is unset or still a placeholder, so there is no server URL to\n"
            "       stamp into the bundle.\n"
            "       The value is the API base the phone uses (scheme, host, port,\n"
            "       no trailing slash) and is deliberately not in this repo.\n"
            "       Put it in %s, which is outside the repo:\n"
            "\n"
            "           mkdir -p %s && chmod 700 %s\n"
            "           printf '%s=http://HOST:8101\\n' >> %s\n"
            "           chmod 600 %s\n"
            "\n"
            "       It must be the same value as the TASKMASTER_API_BASE\n"
            "       repository secret, or a phone taking this bundle loses the\n"
            "       default server it was shipped with."
            % (PUBLIC_BASE_VARS[0], ENV_FILE, ENV_FILE.parent, ENV_FILE.parent,
               PUBLIC_BASE_VARS[0], ENV_FILE, ENV_FILE))
    return value


def next_version(out: Path) -> str:
    """Today's date plus the next free counter, e.g. 2026.0904.3."""
    day = time.strftime(DAY_FMT, time.localtime())
    taken = {p.name for p in out.glob("www-*.zip")}
    n = 1
    while ("www-%s.%d.zip" % (day, n)) in taken:
        n += 1
    return "%s.%d" % (day, n)


def check_source(www: Path) -> None:
    """Refuse anything the phone could not run, before anything is written."""
    index = www / "index.html"
    if not index.is_file():
        die("no index.html at the root of %s — the bundle's entry point is "
            "index.html and the plugin looks for it there" % www)

    text = index.read_text(encoding="utf-8", errors="replace")
    sources = [text]
    sources += [p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(www.rglob("*.js"))]

    if not any(HANDSHAKE in s for s in sources):
        die("%s() is not in %s — without it the plugin's appReadyTimeout "
            "expires and EVERY bundle rolls back at the next launch, silently. "
            "The client must call CapacitorUpdater.notifyAppReady() once it has "
            "painted (capacitor.config.ts, appReadyTimeout)." % (HANDSHAKE, index))

    if PLACEHOLDER not in text:
        die("the literal %s is not in %s — there is nothing to stamp, so the "
            "Settings screen would keep showing whatever bundle string it was "
            "built with and there would be no way to tell from the phone which "
            "bundle it is running. Put <span id=\"bundle\">%s</span> back."
            % (PLACEHOLDER, index, PLACEHOLDER))

    if API_PLACEHOLDER not in text:
        die("the literal %s is not in %s — a bundle with the server URL already "
            "baked in would hand every phone whatever host happened to be in the "
            "working tree. Keep the placeholder (API_DEFAULT) and let this "
            "script stamp it from the environment."
            % (API_PLACEHOLDER, index))


def stage(www: Path, tmp: Path, version: str, api_base: str) -> Path:
    """Copy www/ somewhere writable and stamp the copy: version, then server."""
    staged = tmp / "www"
    shutil.copytree(www, staged, symlinks=False)
    index = staged / "index.html"
    text = index.read_text(encoding="utf-8")
    stamped = text.replace(PLACEHOLDER, version).replace(API_PLACEHOLDER, api_base)
    for literal in (PLACEHOLDER, API_PLACEHOLDER):   # pragma: no cover - paranoia
        if literal in stamped:
            die("%s survived the stamp" % literal)
    index.write_text(stamped, encoding="utf-8")
    return staged


def build_zip(www: Path, dest: Path) -> None:
    """Zip the tree with paths relative to www/ — index.html at the ROOT.

    Not inside a `www/` directory: the plugin unzips the archive and serves what
    it finds, and an extra level would give it a folder where it wanted a page.

    Written deterministically (sorted entries, fixed timestamps, fixed modes) so
    an identical tree produces an identical checksum, and a changed checksum
    always means changed content rather than a changed clock.
    """
    files = sorted(p for p in www.rglob("*") if p.is_file())
    if not files:
        die("%s is empty" % www)
    tmp = dest.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(str(path.relative_to(www)),
                                   date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    tmp.replace(dest)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--www", default=str(REPO / "www"),
                    help="the client to package (default: this repo's www/)")
    ap.add_argument("--bundles-dir",
                    default=os.environ.get("TASKMASTER_BUNDLES_DIR",
                                           str(DEFAULT_BUNDLES)),
                    help="where published bundles live. MUST match the "
                         "server's TASKMASTER_BUNDLES_DIR — a bundle in a "
                         "directory the API does not read is a bundle no phone "
                         "can see (default: %(default)s)")
    ap.add_argument("--version", default=None,
                    help="YYYY.MMDD.n (default: today, next free counter)")
    ap.add_argument("--min-native", default=None,
                    help="lowest native version (the Settings screen's Build, "
                         "1.0.<run number>) this bundle may be served to. Set "
                         "it whenever the bundle needs something the older "
                         "shell has not got. Carried over from the previous "
                         "manifest when omitted; pass '' to clear it")
    ap.add_argument("--notes", default="", help="what changed, for the record")
    ap.add_argument("--keep", type=int, default=5,
                    help="older zips to retain (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every check and print what would be published, "
                         "without writing anything")
    args = ap.parse_args()

    www = Path(args.www).expanduser().resolve()
    out = Path(args.bundles_dir).expanduser().resolve()
    if not www.is_dir():
        die("%s is not a directory" % www)

    check_source(www)
    # Before anything is written, and before --dry-run returns: a dry run that
    # passed and a real run that then refused would be the worst of both.
    api_base = public_base()

    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    # glob on a directory that does not exist yields nothing, so this is the
    # first counter — which is the right answer for a store nobody has used.
    version = args.version or next_version(out)
    if not VERSION_RE.match(version):
        die("%r is not a usable version — it becomes the filename "
            "www-<version>.zip, so no separators and no leading punctuation"
            % version)

    manifest_path = out / "manifest.json"
    try:
        previous = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}

    blob = out / ("www-%s.zip" % version)
    # Only reachable with an explicit --version: next_version() picks a free one.
    if args.version and blob.exists():
        die("%s already exists. A phone has been told that version's checksum; "
            "rewriting the bytes under it would fail its verification. Publish "
            "the next counter instead." % blob)

    # min_native STICKS: a bundle published after a native change still needs
    # the gate, and re-typing it every time is how it gets forgotten.
    if args.min_native is None:
        min_native = previous.get("min_native")
    else:
        min_native = args.min_native.strip() or None

    if args.dry_run:
        print(json.dumps({"would_publish": version, "www": str(www),
                          "bundles_dir": str(out), "min_native": min_native,
                          "api_base_is_set": bool(api_base),
                          "files": len([p for p in www.rglob("*") if p.is_file()])},
                         indent=2))
        return

    with tempfile.TemporaryDirectory(prefix="taskmaster-bundle-") as tmpdir:
        staged = stage(www, Path(tmpdir), version, api_base)
        build_zip(staged, blob)
    checksum = hashlib.sha256(blob.read_bytes()).hexdigest()

    manifest = {
        "version": version,
        "file": blob.name,
        "checksum": checksum,
        "bytes": blob.stat().st_size,
        "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": args.notes,
    }
    if min_native:
        manifest["min_native"] = min_native

    # Atomic. A phone checking mid-write must never read half a manifest — the
    # server treats an unparseable one as "no bundle published", so the cost
    # would be a launch that quietly declines an update that is really there.
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(manifest_path)

    # Keep a few older zips: a download that began before this publish is still
    # in flight, and the previous zip is what publishing-the-old-version-again
    # (the way to undo a bad bundle) needs to still be here.
    stale = sorted((p for p in out.glob("www-*.zip") if p != blob),
                   key=lambda p: p.stat().st_mtime, reverse=True)[args.keep:]
    for path in stale:
        path.unlink()

    print(json.dumps({
        "version": version,
        "checksum": checksum,
        "bytes": manifest["bytes"],
        "path": str(blob),
        "min_native": min_native,
        "bundles_dir": str(out),
        "pruned": [p.name for p in stale],
    }, indent=2))


if __name__ == "__main__":
    main()
