# Build plan — v1

*2026-09-02. Three agents in parallel, then an integration pass by the planner.
Nobody commits: the tree is left staged for the user to review.*

Decisions in force: docs/design.md. Contract: docs/api.md.
Bundle id `org.rightwaytrey.taskmaster`, display name **TaskMaster**,
folder `~/projects/todoapp`, API on `:8101`, default server URL the server's
MagicDNS name on `:8101` — stamped from the `TASKMASTER_API_BASE` secret over
the `__API_BASE__` placeholder, never written into the tree (design.md D4).

## Layout

```
todoapp/
  docs/                 design.md, api.md (contract), build-plan.md, screenshots/
  server/               FastAPI over the `task` CLI          (Agent A)
    app/main.py, app/config.py, app/taskwarrior.py, app/schemas.py, app/routers/
    tests/              pytest against a throwaway TASKDATA, hooks=off
    deploy/             taskmaster-api.service (systemd USER unit), install.sh
    requirements.txt, README.md
  www/index.html        the whole UI, no build step           (Agent B)
  scripts/mock-api.py   in-memory stand-in for the API, for UI work in a browser (Agent B)
  ios/, capacitor.config.ts, package.json     Capacitor shell  (Agent C)
  .github/workflows/testflight.yml            dispatch-only     (Agent C)
  README.md, SHIP.md, CLAUDE.md, .gitignore                     (Agent C)
```

## Agent A — server

Python 3.10 (the host's), FastAPI + uvicorn in a `uv` venv at `server/.venv`
(`~/.local/bin/uv venv --python 3.10 .venv`), style after
`~/projects/carpool/server/` (config, error envelope, deploy files) but far
smaller: no database, no auth tables. Implement every endpoint in docs/api.md
exactly. `app/taskwarrior.py` is the only module that runs `task`
(`~/.local/bin/task`), argv only, one `threading.Lock`, `asyncio.to_thread`.
Tests: `pytest` with a fixture that creates a temp dir, writes a `taskrc`
(`data.location=<dir>`, `hooks=off`) and sets `TASKRC`/`TASKDATA` before the
app is imported, then drives the real `task` binary — no mocks of `task`.
Cover: create (incl. `due:` inside the description staying literal), both due
shapes round-tripping, list/ETag/304, patch with null-clears and tag diffing
with reserved tags ignored, done/undone/409, delete/404, annotate, the IP
allowlist (403 for a `192.168.x` client via `client=` on the ASGI transport),
the token gate. Deploy: `deploy/taskmaster-api.service` (user unit, port 8101,
`Restart=on-failure`, `TZ=America/Chicago`, `EnvironmentFile=-%h/.config/taskmaster/env`)
and `deploy/install.sh` (venv, symlink unit, `systemctl --user daemon-reload
&& enable --now`). **Then actually install and start it** on this box and
prove it against the real Taskwarrior: `curl localhost:8101/health`, list the
real pending tasks, create a task named `taskmaster api smoke test`, watch
`/var/www/dash/todos/data.json` (give it a due of today) update within ~10 s
through the hooks → `pa-pushnow` → `pa retag` path, then DELETE it. Report
the timings and whether the widget feed moved. README: run, test, deploy, the
optional `tailscale serve` HTTPS upgrade and the optional token.

## Agent B — client

`www/index.html`, single file, no framework, no build step, no external
requests, exactly like `~/projects/orderconfirm-ios/www/index.html` and
`~/projects/carpool/www/index.html` (read both first for the conventions:
viewport meta, safe-area insets, light/dark tokens, `esc()` on every string
into innerHTML, `el.hidden` toggling, hash router). Build to docs/api.md.

Screens, bottom tab bar: **Tasks**, **Done**, **Settings**; a task detail
bottom sheet.

- Tasks: header with count and a refresh button plus a small status pill
  (synced HH:MM / offline / N changes waiting). Groups in this order, each
  with a header and only shown when non-empty: **Overdue** (red), **Today**,
  **Upcoming** (by due; label like `Thu Sep 4` or `Sep 12 · 2:30 pm`), **No
  date** (urgency desc). Row: circle checkbox left, description (2-line clamp),
  meta line: project · due · priority badge (H red, M amber, L grey) · tag
  chips (`claude`, `alert`, others) · a "blocked" chip when `blocked`. Tapping
  the circle completes it: instant strike + fade, toast "Completed · Undo" for
  5 s; tapping the row opens the sheet. Quick-add bar pinned above the tab bar:
  text input, a due chip that cycles No date → Today → Tomorrow → Pick date
  (native date input), and Add. Return key adds; the input stays focused so
  several can be entered in a row; the new task appears at once.
- Detail sheet: description (textarea), project (select from meta.projects +
  "New project…"), priority (segmented — / L / M / H), due date (date input)
  and due time (time input, clearable) with a one-line hint: "date only = shows
  in the morning list; add a time = Pushover reminder at that time"; tags as
  toggle chips (`claude`, `alert`, plus any already on the task, plus an "add
  tag" field); annotations read-only with an "Add note" field; Delete (confirm)
  and Save. Un-complete from the Done tab by tapping the check.
- Done: last 30 days grouped Today / Yesterday / date; tap the check to
  un-complete (POST undone).
- Settings: server URL (default `__API_BASE__`, stamped at build time; an
  unstamped build says "not configured" and simply has no default),
  token (optional), "Test connection" (shows /health fields), build stamp
  (`__BUILD__` placeholder in a `<span id="build">`, CI seds it), last
  refresh time, "Clear local cache". Also read `?server=` from the page URL on
  load and store it — that is how the mock and the dev browser are pointed.
- Data layer (design.md D6): localStorage cache of the pending and done lists,
  an ordered write queue (`add`, `patch`, `done`, `undone`, `delete`,
  `annotate`) with temp ids `tmp-…` for adds rewritten to the real uuid on
  success and in any queued ops that reference them; a drain loop that stops
  on a network error and retries on the next refresh; a 4xx drops the op with a
  toast and refreshes. Refresh on load, on `visibilitychange` → visible, every
  30 s while visible, after each successful drain. Use `If-None-Match`.
- `scripts/mock-api.py`: stdlib-only in-memory implementation of docs/api.md
  (enough of it to drive every screen, with a few seeded tasks in each group),
  `python3 scripts/mock-api.py --port 8111`. Serve www with
  `python3 -m http.server 8112 -d www` and open
  `http://localhost:8112/?server=http://localhost:8111`.
- Verify with headless Chrome (`/usr/bin/google-chrome --headless=new
  --screenshot=… --window-size=390,844 --hide-scrollbars <url>`; a
  `?screen=done|settings` param may be used to open a tab for a screenshot):
  Tasks with all four groups, Done, Settings, the sheet if reachable. Save to
  `docs/screenshots/` and look at them — fix what looks wrong before reporting.

## Agent C — shell, CI, docs

Copy the shape of `~/projects/orderconfirm-ios` and `~/projects/carpool`:
`package.json` (`@capacitor/core`, `@capacitor/ios`, dev `@capacitor/cli`,
`typescript`; scripts `sync`, `serve`), `npm install`, `capacitor.config.ts`
(appId `org.rightwaytrey.taskmaster`, appName `TaskMaster`, webDir `www`,
`ios.contentInset: 'automatic'`, `zoomEnabled: false`; no plugins), then
`npx cap add ios` (works on Linux; it needs `www/` to exist — create a
one-line placeholder `www/index.html` **only if the file is absent** and never
touch it otherwise; Agent B owns it). Edit `ios/App/App/Info.plist`:
`CFBundleDisplayName` TaskMaster, `ITSAppUsesNonExemptEncryption` false, and a
scoped `NSAppTransportSecurity` → `NSExceptionDomains` → `__ATS_DOMAIN__`
with `NSIncludesSubdomains` and `NSExceptionAllowsInsecureHTTPLoads` true,
with a comment saying why (design.md D4). Copy `ios/ExportOptions.plist` and
`ios/.gitignore` from carpool. `.github/workflows/testflight.yml`:
`workflow_dispatch` **only** (comment why, citing the August minutes), macos-26,
the transitnav-ios pattern — archive UNSIGNED (no entitlements here, so the
cert-cap dodge applies), export signed with the ASC key, `altool` upload; add a
step before `cap sync` that seds `__BUILD__` in `www/index.html` to
`1.0.<run_number>`; `CURRENT_PROJECT_VERSION`/`MARKETING_VERSION` from
`run_number` as the others do. `.gitignore` after carpool's (node_modules,
build, ios pods/public, server venv, `__pycache__`, `.pytest_cache`), plus
`git init` in the repo root — **no commit**. `README.md`: what it is (two
paragraphs, cite docs/design.md), layout table, run the UI in a browser against
the mock and against the real API, run the server, ship. `SHIP.md`: the
one-time Apple steps in order (App ID `org.rightwaytrey.taskmaster` in the
developer portal, App Store Connect app record — note the record name must be
unique store-wide so "TaskMaster (rwt)" is fine, the display name is separate;
the four repo secrets copied from the other repos; internal tester), then
"every build": dispatch `testflight.yml`, watch, install from TestFlight, set
the server URL if the default is wrong. `CLAUDE.md`: short — conventions, the
"the app means the phone" rule, ask before committing, where the decisions
live, the backlog rule does not apply here (not TransitNav).

## Integration pass (planner)

Run the server tests, run the real service, load `www/index.html` in headless
Chrome against `localhost:8101`, add/complete/undo/delete a task end to end,
`npx cap sync ios`, check `git status` is sane, update README/design if
anything drifted, report to the user with screenshots and the exact remaining
manual steps.
