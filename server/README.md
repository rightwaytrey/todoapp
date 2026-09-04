# TaskMaster server

FastAPI over the `task` CLI. Implements [`docs/api.md`](../docs/api.md) exactly:
base path `/api`, plus an unprefixed `/health`. **No database.** Taskwarrior
3.4.2 on this box is the store and the single source of truth
([`docs/design.md`](../docs/design.md) D1), so there is nothing to migrate,
nothing to back up separately, and everything downstream of Taskwarrior — `pa
roundup`/`digest`/`remind`, the `+claude` queue, the Scriptable home-screen
widget — keeps working untouched.

Python 3.10 compatible; the host runs 3.10.12.

## Run it locally

```bash
cd ~/projects/todoapp/server
~/.local/bin/uv venv --python 3.10 .venv
VIRTUAL_ENV=.venv ~/.local/bin/uv pip install -r requirements.txt

.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8101
```

That runs against the **real** `~/.task` with hooks on, which is what production
does. To poke at it without touching real tasks, point it at a scratch database:

```bash
mkdir -p /tmp/tw/data && printf 'data.location=/tmp/tw/data\nhooks=off\nuda.order.type=numeric\nuda.order.label=Order\n' > /tmp/tw/taskrc
TASKRC=/tmp/tw/taskrc TASKDATA=/tmp/tw/data \
  .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8101
```

Check it is up:

```bash
curl -s http://127.0.0.1:8101/health
# {"ok":true,"version":"0.1.0","time":"…-05:00","tz":"America/Chicago",
#  "task_version":"3.4.2","pending":8}
```

## Tests

```bash
.venv/bin/python -m pytest -q          # 235 tests, ~10 s
```

The suite drives the **real** `task` binary — nothing about Taskwarrior is
mocked, because the only thing worth asserting is the shape of what 3.4.2
actually prints. `tests/conftest.py` creates a throwaway directory, writes a
`taskrc` with `data.location=<tmp>`, `hooks=off` and the `uda.order.*` lines,
exports `TASKRC`/`TASKDATA`/`TASKMASTER_PREFS`
**before the app is imported**, and asserts that the data directory is under the
system temp dir and is not `~/.task` before a single command runs. `hooks=off`
also keeps the suite from firing `pa-pushnow` (and so `task sync` and a widget
republish) a hundred times in four seconds. The prefs file is a throwaway too,
and is deleted between tests — the user's own category order is not a fixture.

Covered: create (including `due:tomorrow` inside a description staying literal,
and shell metacharacters surviving intact), both `due` shapes round-tripping
through CDT *and* CST, list/ETag/304 (weak, list and `*` forms), PATCH with
null-clears and tag diffing that never touches the reserved tags, done/undone
and the 409, delete/404 and the record surviving as `status: deleted`, annotate,
`blocked` from a real dependency, recurring instances listed while the template
is not, the IP allowlist (403 for `192.168.1.5` via `client=` on the ASGI
transport, 200 for `100.x`), and the token gate.

Round 5 adds four files. `test_ordering.py` checks `group_of` and `order_key`
against tables — including the **same 14 cases `www/index.html` was checked
against**, so the two implementations are tested on the same fixtures and not
merely on the same prose — then drives the four sort modes and the `order`
round trip through the API. `test_recurrence.py` pins what Taskwarrior 3.4.2
really does, including the two commands the obvious implementation would have
used and why neither works (`modify recur:` is refused; `modify parent:` makes
a new template that spawns a fresh instance). `test_categories.py` covers the
prefix-match trap (`work` vs `workshop` vs `work.sub`) and a rename over five
tasks, which is what the bulk prompt would silently abandon. `test_widget.py`
covers the group/window/category filters, the caps, and a table of every due
label.

`tests/test_app_update.py` is the exception to "nothing is mocked" — it never
touches Taskwarrior at all, only a throwaway bundles directory. What it asserts
is the *shape* of the live-update answers, because the shape is the whole
contract: an offer carrying an extra key is silently ignored by the plugin, and
a non-offer missing `kind` is read as a broken download. It also covers
`min_native` (withheld, allowed, and an unparseable native version treated as
too old), the download's checksum end to end, every filename the bundle route
must refuse including a symlink out of the directory, and the fact that the
token gate does *not* apply to these two paths.

## Deploy

```bash
bash deploy/install.sh
```

Idempotent, and **no sudo anywhere**: a uv venv, the `uda.order.*` lines
appended to `~/.taskrc` if they are missing (backing the file up first), a
systemd *user* unit copied to `~/.config/systemd/user/`, `daemon-reload`,
`enable --now`, then a `/health` curl it prints. Re-run it after any change.

```bash
systemctl --user status  taskmaster-api
systemctl --user restart taskmaster-api
journalctl  --user -u    taskmaster-api -f
```

It listens on `0.0.0.0:8101`; from the phone that is the server's MagicDNS name
on `:8101` — the value of the `TASKMASTER_API_BASE` repository secret, which is
not in this repo (see **The server's own address** below).

**Why a user unit and not a system one.** The service must inherit the user's
session D-Bus. Taskwarrior's `~/.task/hooks/on-add-pushnow` and `on-modify`
hooks run `systemctl --user start --no-block pa-pushnow.service`, which is
`task sync` → `pa retag` → `task sync` and republishes
`/var/www/dash/todos/data.json`, the widget's feed. A root system unit would
write the task correctly and stop the widget moving, silently. Lingering is
already enabled here, so it starts at boot with nobody logged in.

## Access: the allowlist and the token

Two independent gates, checked in that order by `app/middleware.py` (pure ASGI,
so it runs before routing — an off-net client cannot even provoke a 404):

| | |
|---|---|
| **Address allowlist** | Always on. Loopback + `100.64.0.0/10` (Tailscale's CGNAT range). Anything else gets `403 {"error":"forbidden"}`. Add more with `TASKMASTER_ALLOW_CIDRS=10.0.0.0/24,192.168.1.0/24` (comma-separated). |
| **Bearer token** | Off unless `TASKMASTER_TOKEN` is set. When set, every path except `/health`, `/api/app/update` and `/api/app/bundles/…` needs `Authorization: Bearer <token>` or gets `401`. |

Those last two are exempt because **@capgo/capacitor-updater cannot send an
`Authorization` header** — its only request builder sets `User-Agent`, `Accept`
and `Content-Type` (`CapgoUpdater.swift:207`) and there is no config key for
headers. Gating them would mean a token silently breaking live updates, with the
symptom "updates just don't work" and nothing naming the cause. The address
allowlist still covers them, and what they expose to something already on the
tailnet is a zip of `www/index.html` — the file already inside the app binary.

The allowlist is the real gate. WireGuard already encrypts the path, so this is
HTTP over the tailnet, not HTTP over the internet (`docs/design.md` D4); the
token is defence in depth for a shared box. To turn it on:

```bash
mkdir -p ~/.config/taskmaster
printf 'TASKMASTER_TOKEN=%s\n' "$(openssl rand -hex 24)" > ~/.config/taskmaster/env
chmod 600 ~/.config/taskmaster/env
systemctl --user restart taskmaster-api
```

Then paste the same value into the app's Settings → token. `install.sh` never
writes a token itself.

### The server's own address

This repo is public and the host name is not in it (`docs/design.md` D4). The
value lives in **`~/.config/taskmaster/env`**, which is outside the repo, mode
600, and is already this unit's `EnvironmentFile` (`deploy/taskmaster-api.service`
— the same file the optional bearer token goes in):

```bash
mkdir -p ~/.config/taskmaster
printf 'TASKMASTER_PUBLIC_BASE=http://HOST:8101\n' >> ~/.config/taskmaster/env
chmod 600 ~/.config/taskmaster/env
systemctl --user restart taskmaster-api
```

One line, three readers: this server (`bundle_public_base`, which has **no
default in code** — with it unset every update check answers "no bundle
published" and warns in the journal), `scripts/publish_bundle.py` (which stamps
`__API_BASE__` into the bundle's copy of `index.html` and refuses to publish
without it), and `scripts/ship_web.sh`. It must be the same value as the
`TASKMASTER_API_BASE` repository secret that CI stamps into the shell, or a
bundle hands phones a default server their shell was not built for.

`TASKMASTER_BUNDLE_BASE` is the older name and still wins if both are set.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `TASKMASTER_TOKEN` | *(unset)* | Bearer token. Unset = no auth. |
| `TASKMASTER_ALLOW_CIDRS` | *(unset)* | Extra CIDRs, comma-separated, added to loopback + `100.64.0.0/10`. |
| `TASKMASTER_TASK_BIN` | `~/.local/bin/task`, else `/usr/bin/task` | Which `task` to run. |
| `TASKMASTER_TZ` / `TZ` | `America/Chicago` | The zone every `due` string is in. |
| `TASKMASTER_COMPLETED_DAYS` / `_CAP` | `30` / `200` | The Done tab's window and cap. |
| `TASKMASTER_TASK_TIMEOUT` | `20` | Seconds before a `task` call is abandoned. |
| `TASKMASTER_CORS_ORIGINS` | `*` | Comma list. |
| `TASKMASTER_PREFS` | `~/.config/taskmaster/prefs.json` | The preferences document (see below). Written atomically; a missing or unparseable file reads as the defaults. |
| `TASKMASTER_BUNDLES_DIR` | `server/bundles/` | Where `scripts/publish_bundle.py` writes and `/api/app/update` reads. Publishing to a directory the API does not read is a bundle no phone can see. |
| `TASKMASTER_BUNDLE_BASE` | *(unset — no default in code)* | The absolute base of the download URL handed to the phone. `TASKMASTER_PUBLIC_BASE` is accepted as the same thing. **Must name the same host as the `updateUrl` compiled into the shell** (`capacitor.config.ts`). Unset ⇒ every update check is answered `no bundle published` and a warning goes to the journal. |
| `TASKMASTER_BUNDLE_APP_ID` | `org.rightwaytrey.taskmaster` | Only this app id gets answers. Empty disables the check. |
| `TASKRC` / `TASKDATA` | *(the user's)* | Passed straight through to `task`. Production leaves them unset so the real `~/.taskrc` and its hooks are used. |

## Preferences, ordering, recurrence, categories, the widget feed

*Round 5, 2026-09-04. The contract is `docs/api.md`, "Preferences, ordering,
recurrence, categories, widget"; the reasoning is `docs/design.md` D14 and D15.*

| Endpoint | What it is |
|---|---|
| `GET /api/prefs` | The stored settings document with every default filled in. Never 404s: a box with no file yet answers with the defaults, so "first launch" and "already configured" are one code path on the client. |
| `PUT /api/prefs` | Replaces the whole document. Validated, unknown keys dropped, missing sections defaulted, `422` naming the field. Answers with what was stored. |
| `GET /api/tasks` | Unchanged except that the **pending** half now comes out in display order, and every task carries `group` and `order`. `?status=completed` is untouched (newest `end` first). |
| `PATCH /api/tasks/{uuid}` | Also takes `order`, `recur` and `until`. |
| `GET /api/meta` | Also returns `categories: [{name, count, hidden}]` in prefs order. `count` is pending tasks. |
| `POST /api/categories/rename` | `{"from","to"}` → `204`. Moves every non-deleted task and the preferences with it. |
| `POST /api/categories/delete` | `{"name","move_to"}` → `204`. The tasks survive; only the category goes. |
| `GET /api/widget` | Finished rows for the home-screen widget: text, due label, overdue flag, already sorted and capped. Round 6 adds `prefs.widget.group_by` — `"due"` (round 5, unchanged) or `"category"`, which reorders the same rows into category runs (`categories.order` first, then the rest alphabetically, no category last as `""`) and fills `category` on every row whatever `show_category` says — echoed back as a top-level `group_by`. |

### The preferences file

`~/.config/taskmaster/prefs.json` (`TASKMASTER_PREFS`), beside the `env` file
the unit already reads. It is a plain JSON document and editing it by hand is
supported — there is no cache, so the next request picks it up:

```json
{
  "categories": {"order": ["personal","work","claude","fun","inbox"], "hidden": []},
  "chips":      {"order": ["p:personal","p:work","t:claude","t:alert"], "hidden": []},
  "sort":       {"mode": "due"},
  "widget":     {"groups": ["overdue","today"], "upcoming_days": 7,
                 "category": null,
                 "rows": {"small": 3, "medium": 5, "large": 12},
                 "show_category": false, "group_by": "due"}
}
```

Writes are atomic (a sibling `.tmp`, then one `rename`), so a reader sees the
old document or the new one and never half of one. A file that will not parse —
or that a future version wrote in a shape this one does not understand — is
**logged and ignored**, and the defaults are used: the resting state of a fresh
install is "no file", and one bad byte here must not take the task list down
with it.

### The `order` UDA — and why the server refuses to write it blind

Manual ordering (`sort.mode: "manual"`, drag-to-reorder) persists a position in
a Taskwarrior numeric UDA. `deploy/install.sh` adds it to `~/.taskrc` if it is
not there, backing the file up first:

```
uda.order.type=numeric
uda.order.label=Order
```

It has no `urgency.uda.order.coefficient`, so it contributes 0 to urgency —
adding it moves no task's urgency and reorders no report.

**Without those two lines, `PATCH {"order": …}` answers `409` rather than
running.** That is not caution. On Taskwarrior 3.4.2, with the UDA undeclared:

```
$ task <uuid> modify order:1500      # exit 0 — and now the DESCRIPTION is "order:1500"
$ task <uuid> modify order:           # exit 0 — and now it is "order:"
```

The token is not an attribute Taskwarrior knows, so it falls through to the
description. One drag would shred a screenful of real tasks, silently. The
server probes `task _get rc.uda.order.type` before every `order` write, for the
clear as well as the set, and says which two lines are missing.

### Recurrence, and what Taskwarrior will not let go of

`PATCH` takes `recur` (a period, or `null` to stop) and `until`. Because
Taskwarrior's model is a `status:recurring` **template** plus one pending
instance, and the phone only ever sees instances, the server redirects:

- **Instance → template.** `recur`/`until` are written to the parent, and read
  back from it too: an instance's own `recur`/`until` are a snapshot from when
  it was spawned and do **not** follow a template edit.
- **Plain task → template.** `recur:<period>` promotes the task itself; the
  response is the **first instance** Taskwarrior spawns, which has a different
  uuid. No `due` → `422`.
- **Stop repeating** deletes the template and leaves the instance alone. It
  cannot do more: `modify recur:` on an instance is refused outright, and
  `modify parent:` promotes the instance into a *new live template* that spawns
  a fresh instance — the opposite of stopping. So Taskwarrior's own record keeps
  a dead `recur` and a `parent` pointing at a deleted task, and the API reports
  such a row as the plain task it now behaves like. `task <uuid> info` in the
  terminal still shows the residue.

All of it is pinned by `tests/test_recurrence.py` against the real binary; the
full findings are in `docs/api.md` under Recurrence.

### Category rename and delete

Both are one bulk `modify` over pending **and** completed tasks, leaving deleted
ones as history, plus a rewrite of the preferences (order, hidden, the
`p:<name>` filter chips, and `widget.category`). Two flags carry it:

* **`project.is:<name>`, never `project:<name>`.** Taskwarrior attribute filters
  are prefix matches, so `project:work` also selects `workshop` and `work.sub` —
  silent data loss on the real database.
* **`rc.bulk=0`.** `rc.confirmation=off` does not cover the bulk prompt: over
  five tasks the modify asks, reads EOF, and exits 1 having changed nothing.

## App bundle updates (`docs/design.md` D11)

The server is also the shell's live-update store: the phone's
`@capgo/capacitor-updater` asks it at each cold launch whether a newer
`www/index.html` exists, and downloads the zip from here if one does. That is
what makes a change to the client cost nothing instead of ~30 billed macOS
minutes. Two endpoints, `app/routers/app_update.py`, and the exact wire protocol
— cited to plugin source lines — is in [`docs/api.md`](../docs/api.md).

| | |
|---|---|
| `POST /api/app/update` | The plugin's check. Always 200: an offer is `{version, url, checksum}` and **only** those, a non-offer is `{message, kind}` and the `kind` is not optional. |
| `GET /api/app/update` | A status view for humans and `scripts/ship_web.sh`. Never called by the plugin, and deliberately a different shape so it cannot be mistaken for an offer. |
| `GET /api/app/bundles/www-<version>.zip` | The zip. Any other filename is a 404. |

Bundles live in `server/bundles/` (`TASKMASTER_BUNDLES_DIR`), gitignored, with
`manifest.json` naming the current one. Nothing here writes them —
`scripts/publish_bundle.py` does:

```bash
python3 scripts/publish_bundle.py --www www --notes "fix the Done tab"
python3 scripts/publish_bundle.py --www www --bundles-dir /tmp/try --dry-run
curl -s http://127.0.0.1:8101/api/app/update | python3 -m json.tool   # what is published
journalctl --user -u taskmaster-api -f | grep app-update              # who is asking
```

The store is currently **empty on purpose**, and it answers every phone
`{"message": "no bundle published", "kind": "up_to_date"}` — quietly, at info
level, which is why "no manifest" is `up_to_date` and not `failed`. Nothing
should be published until a TestFlight build carrying the plugin is installed;
before that no phone is asking, and a bundle that assumes a native change an
installed shell lacks is what `min_native` exists to withhold.

Adding a bundle does not need a restart — the manifest is read per request. A
change to this code does: `systemctl --user restart taskmaster-api`.

## Optional: HTTPS through `tailscale serve`

Plain HTTP is fine over WireGuard, and it is what the app ships with. If you
want TLS anyway (one command, needs root — `sudo -n -l` on this box does allow
`systemctl`/`iptables`/`docker`/`nginx` but not `tailscale`, so this is a
by-hand step):

```bash
sudo tailscale serve --bg --https=8445 http://127.0.0.1:8101
sudo tailscale serve status
```

Tailscale terminates TLS with a real Let's Encrypt certificate for the MagicDNS
name. The app's server URL then becomes:

```
https://<the server's MagicDNS name>:8445
```

— note **https**, the **8445** port, and the full MagicDNS name (the certificate
is issued for that name, so an IP or a short hostname will fail validation).
Change it in the app's Settings screen (and the `TASKMASTER_API_BASE` secret,
and `TASKMASTER_PUBLIC_BASE` in `~/.config/taskmaster/env`, so a store build and
an over-the-air bundle agree with it); nothing on the server changes, and `:8101`
keeps working alongside. The `Info.plist` ATS exception for the tailnet domain
becomes unnecessary but is harmless. To undo:
`sudo tailscale serve --https=8445 off`.

## Troubleshooting

**`403 {"error":"forbidden"}` — you are not on the tailnet.** The request's
source address was neither loopback nor in `100.64.0.0/10`. On the phone: is
Tailscale connected? Are you on the LAN's Wi-Fi with the VPN off, so the box
sees a `192.168.x` address? `journalctl --user -u taskmaster-api | grep refused`
names the address it saw. If you genuinely want a LAN client, add its subnet to
`TASKMASTER_ALLOW_CIDRS`.

**`401 {"error":"unauthorized"}`** — a token is configured but the request did
not carry a matching `Authorization: Bearer …`. Compare
`~/.config/taskmaster/env` with the app's Settings screen. `/health` still
answers, so use it to confirm the server is otherwise fine.

**`502 {"error":"task_failed"}` — read the `detail`.** It is Taskwarrior's own
words, verbatim: the CLI exited non-zero and this is what it said. (3.4.2 prints
its complaints on stdout rather than stderr, so `detail` takes stderr when there
is one and stdout otherwise.) Reproduce it by hand with the same arguments —
`journalctl --user -u taskmaster-api` logs every failure — and if `detail` is
about a lock, something else is holding `~/.task` open.

**`/health` says `ok: false`** — the `task` binary could not be run at all;
`detail` says why. Usually `TASKMASTER_TASK_BIN` pointing somewhere wrong, or a
`~/.taskrc` that fails to parse.

**A change does not reach the phone widget.** That path is Taskwarrior's hooks,
not this server: `on-add-pushnow` → `pa-pushnow.service` → `task sync` → `pa
retag` → `/var/www/dash/todos/data.json`. Check
`systemctl --user status pa-pushnow` and the `updated` field in that file. If
the server is running as anything other than the login user it has no session
bus and the hook's `systemctl --user` call is a no-op.

**The service will not start.** `journalctl --user -u taskmaster-api -n 50`.
An `Address already in use` means something else took 8101 (8095–8100 are
already spoken for on this box); `ss -ltnp | grep 8101` names it.

**A published bundle never reaches the phone.** `journalctl --user -u
taskmaster-api | grep app-update` has one line per check, saying what that
device was running and what it was told. `no bundle published` with something in
`server/bundles/` means the service is reading a different directory — the
startup line logs which (`bundles=…`), and `TASKMASTER_BUNDLES_DIR` is what
moves it. `needs native …` is `min_native` doing its job. No lines at all means
the phone is not asking: either it is off the tailnet, or the installed build
predates the updater plugin, which is the usual answer.

## Shape of the thing

```
app/
  config.py        env -> Settings; the allowlist CIDRs, the pa project list,
                   and RESERVED_TAGS all live here
  errors.py        the {"error","detail"} envelope, and TaskFailed
  taskwarrior.py   the ONLY module that runs `task`: argv (never a shell), one
                   threading.Lock, asyncio.to_thread, rc.context=none
  serialize.py     export dict -> the Task object; the two `due` shapes; and
                   the canonical order — group_of / order_key / sort_key /
                   due_label, the one implementation of docs/design.md D8
  schemas.py       pydantic v2 request bodies (unknown keys ignored)
  prefs.py         the preferences document: pydantic models, defaults, and
                   the atomic read/write of prefs.json
  middleware.py    the address allowlist + bearer token, pure ASGI
  routers/         health (unprefixed), meta, tasks, prefs, categories,
                   widget, app_update (the shell's live-update check and
                   bundle download — nothing to do with Taskwarrior; the
                   phone's WebView never calls it)
  main.py          the app, the four exception handlers, CORS
tests/             pytest against a throwaway TASKDATA, the real `task`
deploy/            taskmaster-api.service (user unit), install.sh
```

Three decisions worth not re-deriving, all commented at the source:

* **`rc.context=none` on every invocation.** `~/.taskrc` defines read-contexts.
  If one is left applied (`task context work`), an unscoped
  `status:pending export` silently returns only the in-context tasks — and the
  phone's list is "every pending task, no filter" (`docs/design.md` D2). `pa`
  forces it for the same reason.
* **One lock, and `asyncio.to_thread`.** Taskwarrior 3 keeps one SQLite file and
  takes its own write lock; overlapping `task` processes fail rather than queue.
  Each call is ~10 ms, so a single process-wide lock is enough — which is why
  the unit runs **one** uvicorn worker.
* **Reserved tags are stripped.** `today`, `overdue` and `due` are painted onto
  pending tasks by `pa retag` for the *old* Taskchamp filter. They are
  maintenance state, not user data: the server removes them from every `tags`
  array it returns and ignores them in every one it receives, and a `PATCH`
  that replaces `tags` diffs only against the non-reserved set, so the phone can
  never delete state the desktop owns. `next`, `nocal`, `nocolor` and `nonag`
  are ordinary tags and pass straight through.
