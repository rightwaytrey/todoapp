#!/usr/bin/env bash
#
# Install and start the TaskMaster API on this box. Idempotent — run it again
# after every change to the server and it will re-sync deps, re-install the
# unit, restart and re-check.
#
#   bash deploy/install.sh
#
# **No sudo anywhere.** Everything lives under $HOME: a uv venv in server/.venv,
# a systemd *user* unit in ~/.config/systemd/user/, an unprivileged port (8101).
# The one thing that would need root — `loginctl enable-linger` for start-at-
# boot — is already enabled on the home server, and this script only warns if it
# is not.
#
# It deliberately touches nothing else: not nginx, not tailscale, not the
# firewall, and none of the pa-* units. The service reaching pa-pushnow through
# Taskwarrior's hooks is a property of running as the user, not of any wiring
# this script does (see deploy/taskmaster-api.service).
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJ/.venv"
SERVICE="taskmaster-api"
PORT="${PORT:-8101}"
UV="${UV:-$HOME/.local/bin/uv}"
UNIT_DIR="$HOME/.config/systemd/user"

cd "$PROJ"
[ -f "$PROJ/app/main.py" ] || { echo "ERROR: app/main.py not found in $PROJ"; exit 1; }

echo "==> 1/5  Python venv + dependencies"
if [ -x "$UV" ]; then
  [ -d "$VENV" ] || "$UV" venv --python 3.10 "$VENV"
  VIRTUAL_ENV="$VENV" "$UV" pip install --quiet -r "$PROJ/requirements.txt"
else
  echo "    uv not at $UV -- falling back to python3 -m venv"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$PROJ/requirements.txt"
fi
echo "    $("$VENV/bin/python" -V), uvicorn $("$VENV/bin/uvicorn" --version 2>&1 | tail -1)"

echo "==> 2/5  Taskwarrior"
TASK="${TASKMASTER_TASK_BIN:-$HOME/.local/bin/task}"
[ -x "$TASK" ] || TASK=/usr/bin/task
if [ -x "$TASK" ]; then
  echo "    $TASK $("$TASK" --version)  (data: ${TASKDATA:-$HOME/.task})"
else
  echo "    WARNING: no task binary found; /health will answer ok:false."
fi

echo "==> 3/5  environment file (optional)"
# Never written with a value. Auth is off by default on purpose: the tailnet
# allowlist is the gate (docs/design.md D4), and a token this script invented
# would be a secret nobody chose sitting in a backup nobody audits.
ENV_FILE="$HOME/.config/taskmaster/env"
if [ -r "$ENV_FILE" ]; then
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "    using $ENV_FILE"
else
  echo "    none at $ENV_FILE -- running with no token and the default allowlist."
  echo "    To add one:  mkdir -p ~/.config/taskmaster"
  echo "                 printf 'TASKMASTER_TOKEN=%s\\n' \"\$(openssl rand -hex 24)\" > $ENV_FILE"
  echo "                 chmod 600 $ENV_FILE && systemctl --user restart $SERVICE"
fi

echo "==> 4/5  systemd USER service ($SERVICE)"
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "    NOTE: lingering is off, so this will not start at boot."
  echo "          One command, needs root, not run here: sudo loginctl enable-linger $USER"
fi
mkdir -p "$UNIT_DIR"
# Copied rather than symlinked: a symlink into the repo makes `systemctl --user
# enable` follow the link and record the repo path in the .wants dir, so moving
# or removing the checkout leaves a dangling enabled unit.
install -m 644 "$PROJ/deploy/$SERVICE.service" "$UNIT_DIR/$SERVICE.service"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE" >/dev/null 2>&1 || true
systemctl --user restart "$SERVICE"
echo "    $(systemctl --user is-enabled "$SERVICE" 2>/dev/null), $(systemctl --user is-active "$SERVICE" 2>/dev/null)"

echo "==> 5/5  health check"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
echo -n "    GET /health -> "
curl -sS "http://127.0.0.1:$PORT/health" || echo "(no response)"
echo

echo
echo "Listening on 0.0.0.0:$PORT. From the phone, on the tailnet:"
echo "    http://<the server's MagicDNS name>:$PORT (the TASKMASTER_API_BASE secret)"
echo "Logs:     journalctl --user -u $SERVICE -f"
echo "Restart:  systemctl --user restart $SERVICE"
