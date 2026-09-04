#!/usr/bin/env bash
# Ship a change to www/index.html to the phone — no store build, no CI minutes.
#
#   scripts/ship_web.sh "what changed"
#
# Steps:
#   1. Publish www/ as a bundle into the store the API reads
#      (scripts/publish_bundle.py, which stamps __BUNDLE__ in a COPY and refuses
#      a client that never calls notifyAppReady).
#   2. Prove it end to end against the SAME url the phone uses: ask for an
#      update the way the plugin does, download what is offered, check the
#      SHA-256 and that index.html is at the zip root — which is exactly what
#      @capgo/capacitor-updater does before installing a bundle.
#
# The phone applies it at its next COLD launch: `autoUpdate: 'onLaunch'` is a
# direct-update mode in Capgo 8.51.15, so the first check of a process downloads
# and reloads inside that same launch (see capacitor.config.ts). An app that is
# already running only queues the bundle and swaps at the next launch.
#
# What this CANNOT ship: anything native — a Capacitor plugin, an Info.plist
# key, the widget, the updater's own config. Those need a TestFlight dispatch
# (SHIP.md), and a bundle that DEPENDS on one must be gated:
#
#   MIN_NATIVE=1.0.9 scripts/ship_web.sh "uses the new bridge"
#
# Prerequisite that is easy to forget: the installed shell must already carry
# the plugin. Until a store build made after 2026-09-04 is on the phone, every
# bundle published here is inert — nothing on the device is asking.
set -euo pipefail

cd "$(dirname "$0")/.."

NOTES="${1:-}"
# The name the shell's native updateUrl points at (capacitor.config.ts). Not
# 127.0.0.1: this is the host the PHONE resolves, and the offer's download URL
# has to be on it or the download 404s and reads as a checksum failure.
#
# It is NOT in this repo — it names the user's own server (design.md D4). It
# comes from the environment, or from ~/.config/taskmaster/env, which is the
# same file the systemd unit reads (deploy/taskmaster-api.service,
# EnvironmentFile) and lives outside the repo at mode 600. The same value is
# the TASKMASTER_API_BASE repository secret on the CI side, and the same value
# scripts/publish_bundle.py stamps into the bundle.
ENV_FILE="$HOME/.config/taskmaster/env"
if [ -z "${TASKMASTER_API:-}" ] && [ -z "${TASKMASTER_PUBLIC_BASE:-}" ] \
   && [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
API="${TASKMASTER_API:-${TASKMASTER_PUBLIC_BASE:-${TASKMASTER_BUNDLE_BASE:-}}}"
API="${API%/}"
if [ -z "$API" ]; then
  echo "TASKMASTER_PUBLIC_BASE is not set, so there is no server to publish to." >&2
  echo "It is the API base the phone uses (scheme, host, port, no trailing" >&2
  echo "slash) and is deliberately not in this repo. Put it in $ENV_FILE:" >&2
  echo >&2
  echo "    mkdir -p \"\$(dirname \"$ENV_FILE\")\"" >&2
  echo "    printf 'TASKMASTER_PUBLIC_BASE=http://HOST:8101\\n' >> \"$ENV_FILE\"" >&2
  echo "    chmod 600 \"$ENV_FILE\"" >&2
  echo "    systemctl --user restart taskmaster-api" >&2
  exit 1
fi
APP_ID="$(grep -oP "appId:\s*'\K[^']+" capacitor.config.ts)"

echo "==> Publishing www/ …"
ARGS=(--www www --notes "$NOTES")
[ -n "${BUNDLES_DIR:-}" ] && ARGS+=(--bundles-dir "$BUNDLES_DIR")
[ -n "${MIN_NATIVE:-}" ] && ARGS+=(--min-native "$MIN_NATIVE")
[ -n "${VERSION:-}" ] && ARGS+=(--version "$VERSION")
PUBLISHED="$(python3 scripts/publish_bundle.py "${ARGS[@]}")"
echo "$PUBLISHED"
VERSION="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])' <<<"$PUBLISHED")"
CHECKSUM="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["checksum"])' <<<"$PUBLISHED")"

echo "==> Verifying through $API (what the phone actually does)"
# Pose as a shell running an older bundle. The answer must carry version + url +
# checksum and NO message key — the plugin reads a message as "nothing to do".
OFFER="$(curl -sS --max-time 20 -X POST "$API/api/app/update" \
  -H 'content-type: application/json' \
  -d "{\"app_id\":\"$APP_ID\",\"platform\":\"ios\",\"device_id\":\"ship-web-check\",\"version_name\":\"0.0.0\",\"version_build\":\"999.0.0\"}")"
echo "    offer: $OFFER"

OFFERED_VERSION="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("version",""))' <<<"$OFFER")"
OFFERED_URL="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("url",""))' <<<"$OFFER")"
[ "$OFFERED_VERSION" = "$VERSION" ] || {
  echo "FAIL: the API offers '$OFFERED_VERSION', not '$VERSION'."
  echo "      Most likely its TASKMASTER_BUNDLES_DIR is not the directory just"
  echo "      written to. Check: curl -s $API/api/app/update"
  exit 1; }
# The download URL comes from the server's TASKMASTER_BUNDLE_BASE, not from the
# host that answered, so a stale value hands out URLs for a box the phone cannot
# reach — and the phone would report that as a checksum failure. Name it here.
case "$OFFERED_URL" in
  "$API"/*) ;;
  *) echo "FAIL: $API offered a bundle hosted at $OFFERED_URL."
     echo "      The server's TASKMASTER_BUNDLE_BASE disagrees with the host this"
     echo "      script (and the phone) uses, or is unset — an unset one makes the"
     echo "      server answer 'no bundle published' whatever is on disk."
     echo "      Fix it in $ENV_FILE and systemctl --user restart taskmaster-api"
     exit 1;;
esac

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -sS --max-time 120 -o "$TMP/bundle.zip" "$OFFERED_URL"
DOWNLOADED="$(sha256sum "$TMP/bundle.zip" | cut -d' ' -f1)"
[ "$DOWNLOADED" = "$CHECKSUM" ] || {
  echo "FAIL: downloaded $DOWNLOADED, published $CHECKSUM"; exit 1; }
python3 - "$TMP/bundle.zip" "$VERSION" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
if "index.html" not in z.namelist():
    sys.exit("FAIL: the served zip has no index.html at its root")
if sys.argv[2].encode() not in z.read("index.html"):
    sys.exit("FAIL: the served index.html is not stamped %s" % sys.argv[2])
PY

# The other half of the plugin's contract, and the half that is silent when it
# is wrong: a response that is NOT an offer must carry "kind", or the plugin
# falls through to an empty download url and logs an error once per launch on a
# perfectly healthy phone.
UPTODATE="$(curl -sS --max-time 20 -X POST "$API/api/app/update" \
  -H 'content-type: application/json' \
  -d "{\"app_id\":\"$APP_ID\",\"platform\":\"ios\",\"device_id\":\"ship-web-check\",\"version_name\":\"$VERSION\",\"version_build\":\"999.0.0\"}")"
python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("kind") else "FAIL: the up-to-date answer carries no kind: %s" % d)' <<<"$UPTODATE"

echo
echo "Shipped $VERSION ($(du -h "$TMP/bundle.zip" | cut -f1), $CHECKSUM)."
echo "Phones pick it up at their next cold launch. Zero Actions minutes spent."
echo "Watch them ask:  journalctl --user -u taskmaster-api -f | grep app-update"
