# TaskMaster API contract (v1)

*Written 2026-09-02 by the planner so the server and the client can be built in
parallel. Both sides build to this. If you must change it, change this file
first and say so in your report.*

The server is a thin HTTP layer over the `task` CLI on the home server
(docs/design.md D1). It listens on `0.0.0.0:8101`, JSON everywhere, base path `/api`.

**Time.** The server's zone is `America/Chicago`. Instants (`*_at`, `entry`,
`modified`, `end`) are ISO-8601 with offset. The `due` field is a **local
wall-clock string** in one of two shapes, and the shape is meaningful
(design.md D3):

- `"YYYY-MM-DD"` — date-only (Taskwarrior local midnight). Digest only, no ping.
- `"YYYY-MM-DDTHH:MM"` — clocked. `pa remind` pings when it comes due.

The server decides the shape on the way out: a due whose local time is exactly
00:00:00 is date-only. On the way in the client sends the same two shapes.

**Access.** Requests from any client address outside loopback or
`100.64.0.0/10` (the tailnet) get `403 {"error":"forbidden"}` before anything
else runs. If the env var `TASKMASTER_TOKEN` is set, every path except
`/health` also requires `Authorization: Bearer <token>` (`401` otherwise). If it
is unset, no auth. CORS: allow any origin, methods `GET,POST,PUT,PATCH,DELETE,
OPTIONS`, headers `Authorization, Content-Type, If-None-Match`, expose `ETag`
— the app runs at `capacitor://localhost` and the dev browser at
`http://localhost:*`.

**Errors.** `{"error": "<machine_code>", "detail": "<human text>"}` with a
4xx/5xx. Codes: `bad_request` (400), `unauthorized` (401), `forbidden` (403),
`not_found` (404), `invalid_request` (422, with `detail` naming the field),
`task_failed` (502 — the `task` CLI returned non-zero; `detail` carries its
stderr, trimmed — **or its stdout when stderr is empty**, because Taskwarrior
3.4.2 prints user-facing errors on stdout: `task <uuid> done` on a completed
task exits 1 with "…is neither pending nor waiting." on stdout and nothing on
stderr, so stderr alone would leave `detail` empty exactly when it matters).
A body key the server does not know is **ignored**, not rejected — the contract
lists what is accepted and never promised to refuse anything else.

## Health

`GET /health` → `200 {"ok": true, "version": "0.1.0", "time": <iso>, "tz":
"America/Chicago", "task_version": "3.4.2", "pending": <int>}`.
Unauthenticated even when a token is configured. `ok` is false (still 200) if
`task` cannot be run; then `task_version` is null and `detail` says why.

## Meta

`GET /api/meta` → `{"projects": [..], "tags": [..], "priorities": ["H","M","L"],
"tz": "America/Chicago", "now": <iso>}`.

`projects`: the union of `personal, work, claude, fun, inbox` (the `pa`
projects, always present, in that order) and every other project name in use
on any task, sorted after them. `tags`: user tags in use on pending tasks,
minus the reserved maintenance tags (below), sorted.

## Tasks

### The Task object

```
{
  "uuid":        "a96ce075-…",
  "description": "water the plants",    // may contain newlines
  "status":      "pending" | "completed" | "deleted" | "waiting" | "recurring",
  "project":     "personal" | null,
  "priority":    "H" | "M" | "L" | null,
  "due":         "2026-09-03" | "2026-09-03T14:30" | null,   // local, see Time
  "due_at":      "2026-09-03T00:00:00-05:00" | null,          // the same instant
  "tags":        ["claude"],            // user tags, reserved tags stripped
  "annotations": [{"entry": <iso>, "text": "claude: looked into it"}],
  "recur":       "daily" | null,        // the TEMPLATE's schedule, read through
                                        // `parent` — see Recurrence, round 5
  "until":       "2026-12-01" | null,   // likewise
  "parent":      "62c4db7e-…" | null,
  "group":       "overdue"|"today"|"upcoming"|"none",   // round 5
  "order":       1500 | null,           // the manual-order UDA; round 5
  "depends":     ["<uuid>", …],         // may be empty
  "blocked":     false,                 // true when any dependency is still pending
  "urgency":     13.48,
  "entry":       <iso>,
  "modified":    <iso>,
  "end":         <iso> | null           // completion time; Taskwarrior also
                                        // sets it on delete, and it is passed
                                        // through for deleted tasks too
}
```

**Reserved tags** `today`, `overdue`, `due` are maintained by `pa retag` for
the old phone filter. The server strips them from every `tags` array it returns
and ignores them in every `tags` array it receives; the client never sees them.
`next`, `nocal`, `nocolor`, `nonag` are ordinary Taskwarrior special tags and
pass through.

`id` (Taskwarrior's renumbering integer) is deliberately absent. Everything is
addressed by `uuid`.

### List

`GET /api/tasks` → `200 [<Task>]`.
`?status=pending` (default) → every pending task, no filter, no context
(`rc.context=none`), recurring **templates** excluded (they are
`status:recurring`; their instances are pending and included). ~~Sorted by
`urgency` desc — the client regroups anyway.~~ **Superseded in round 5:** the
pending half comes out in display order, and each task carries `group` and
`order`. See "Ordering moves to the server" below.
`?status=completed` → tasks completed in the last 30 days, newest `end` first,
capped at 200.
`?status=all` → both, pending first.

Responds with an `ETag` (a hash of the body). A request with a matching
`If-None-Match` gets `304` and no body. The client polls this every 30 s while
visible, so this is what keeps that cheap.

### Create

`POST /api/tasks` `{"description": "call bob", "project"?: string|null,
"priority"?: "H"|"M"|"L"|null, "due"?: string|null, "tags"?: [string]}`
→ `201 <Task>`.

`description` is required, trimmed, non-empty. It is passed to `task add` after
`--`, so text like `due:tomorrow` inside it stays literal. `project` matches
`^[A-Za-z0-9_.-]{1,40}$`; `tags` entries match `^[A-Za-z0-9_]{1,40}$` (reserved
ones dropped silently); `due` must be one of the two shapes above. Anything
else is `422 invalid_request` naming the field. Omitted fields are simply not
set.

### Read one

`GET /api/tasks/{uuid}` → `200 <Task>` or `404`. Any status, including deleted.

### Update

`PATCH /api/tasks/{uuid}` — any subset of `description, project, priority, due,
tags` → `200 <Task>`.

Same validation as create. `null` **clears** the field (`project:`, `priority:`,
`due:` in Taskwarrior terms); an omitted field is untouched. `tags` is the full
replacement set of user tags: the server diffs it against the task's current
non-reserved tags and applies `+x` / `-y` — reserved tags are never touched.
`description` is passed after `--`.

`description` and `tags` are the two exceptions to the null rule and reject
`null` with `422`: Taskwarrior has no empty-description task (`modify -- ""` is
a silent no-op), and `tags` is a replacement set, so `[]` is how it is cleared.

### Complete / un-complete

`POST /api/tasks/{uuid}/done` → `200 <Task>` (status `completed`, `end` set).
Runs `task <uuid> done`. On a recurring instance Taskwarrior spawns the next
instance itself; the client sees it on the next list. **Idempotent**: on a task
that is already `completed` the server skips the command and returns the task
as it stands, so a write replayed from the offline queue (design.md D6) settles
instead of raising. (`task done` on a completed task exits 1.)

`POST /api/tasks/{uuid}/undone` → `200 <Task>` (status `pending`, `end` null).
Runs `task <uuid> modify status:pending` (verified: clears `end`). `409
{"error":"conflict"}` if the task is not `completed`.

### Delete

`DELETE /api/tasks/{uuid}` → `204`. Runs `task <uuid> delete` (confirmation
off). Taskwarrior keeps the record with `status: deleted`; `GET /api/tasks/{uuid}`
still returns it. Idempotent — a second `DELETE` is another `204`. `404` if the
uuid does not exist at all.

A `{uuid}` path parameter that is not a full 36-character uuid is `404` on every
route. It is not pedantry: the value is used as a Taskwarrior *filter*, and
`task status:pending modify project:x` would rewrite every pending task, so
anything that is not an exact uuid is refused before it reaches an argv slot.
Partial uuids are refused too — Taskwarrior would prefix-match them.

### Annotate

`POST /api/tasks/{uuid}/annotations` `{"text": "…"}` → `200 <Task>`. Trimmed,
non-empty. Runs `task <uuid> annotate -- <text>`.

## How the server runs `task`

Every invocation is
`task rc.confirmation=off rc.recurrence.confirmation=off rc.context=none
rc.verbose=nothing <filter…> <command> <args…>` (with `rc.verbose=new-uuid` for
`add`, whose stdout is then `Created task <uuid>.`), argv only, never a shell,
serialised behind one lock and run off the event loop. Filters address tasks by
uuid, never by integer id. `TASKRC`/`TASKDATA` come from the environment so the
tests can point at a throwaway data dir with `hooks=off`; production runs with
the user's real `~/.taskrc`, hooks on — those hooks are what fire `pa-pushnow`
and keep the Scriptable widget live (design.md D1).

Dates out: Taskwarrior exports `YYYYMMDDTHHMMSSZ` (UTC); convert to
`America/Chicago` to build `due`/`due_at`. Dates in: pass `due:YYYY-MM-DD` or
`due:YYYY-MM-DDTHH:MM` verbatim — Taskwarrior reads both as local time
(verified: `due:2026-09-05T14:30` → `20260905T193000Z` in CDT).

## Canonical order (design.md D8)

Both clients show pending tasks in this order and nothing else decides it:

1. **Group**: overdue, today, upcoming, no date — as `www/index.html`'s
   `groupOf()` defines them (date part of `due` vs the local date; a clocked due
   today is overdue once its time has passed; a date-only due today is not).
2. Within a group, by the string key
   `<date> + "T" + (<HH:MM> or "99:99") + <prio> + <uuid>` where `<date>` is the
   `due` date part (`""` for no date), the time is `99:99` for a date-only due
   so it sorts after every clocked due of that day, and `<prio>` is
   `0` for H, `1` for M, `2` for L, `3` for none.
3. The **no date** group alone sorts by `urgency` descending first, then the
   same key, because there is no date to lead with.

The server keeps returning `urgency` order; the clients sort. The widget shows
only the overdue and today groups, in exactly this order, so its rows are the
top of the app's list.

## App bundle updates (design.md D11)

*This is not our contract — it is `@capgo/capacitor-updater`'s, as **8.51.15**
actually implements it (the version in `package-lock.json`). Every claim below
was read out of `node_modules/@capgo/capacitor-updater/ios/Sources/
CapacitorUpdaterPlugin/` on 2026-09-04 and is cited to a line. Re-read it after
a plugin upgrade; the protocol is undocumented and has changed before.*

These endpoints are for the **native shell**, not the client. Nothing in
`www/index.html` calls them; the plugin does, from Swift, at each cold launch.

### The check

`POST /api/app/update` → always **200**. A failed update check must never look
like a broken app: the phone keeps the bundle it has and asks again next launch.

**Request** — `InfoObject.toParameters()` (`CapgoUpdater.swift:1018`), a flat
JSON object. Every field is optional and the server reads four:

| Field | What it is | Used for |
|---|---|---|
| `app_id` | `org.rightwaytrey.taskmaster` | refuse another app |
| `version_name` | the **bundle** the phone is running | "up to date" |
| `version_build` | the **native** version | `min_native` gating |
| `device_id` | per-install uuid | the log line, nothing else |

`platform`, `version_code`, `version_os`, `plugin_version`, `is_emulator`,
`is_prod`, `install_source`, `custom_id`, `channel`, `defaultChannel`, `key_id`
are also sent and ignored here.

Two traps in those two version fields:

- `version_name` is the bundle version (`2026.0904.1`) — **except** on a shell
  that has never taken an update, where `getBundleInfo(id:)` returns the native
  marketing version instead (`CapgoUpdater.swift:3397`), so a fresh install
  reports `"1.0.7"`, not `"builtin"`. Two namespaces share one field, which is
  why bundle versions are date-shaped and can never collide with `1.0.<n>`.
- `version_build` is `CFBundleShortVersionString` = `MARKETING_VERSION` =
  `1.0.<run number>` (`CapacitorUpdaterPlugin.swift:268`), the number the
  Settings screen shows as **Build**. Not `CFBundleVersion`, which arrives
  separately as `version_code`.

The body is parsed leniently — a malformed one is treated as `{}` rather than
answered with a 422, because a 422 here is an update check that looks like a
broken app.

**Response, when a bundle is offered** — exactly these three keys:

```json
{"version": "2026.0904.1",
 "url": "http://HOST:8101/api/app/bundles/www-2026.0904.1.zip",
 "checksum": "e4f331ef…"}
```

`checksum` is the SHA-256 of the zip, lowercase hex; the plugin verifies it
before installing. `url` is absolute — the app runs at `capacitor://localhost`,
where a relative URL would resolve against the app itself — and comes from
`TASKMASTER_BUNDLE_BASE` (or `TASKMASTER_PUBLIC_BASE`), which **must name the
same host as the `updateUrl` compiled into the shell**. It has **no default in
code**, because it is the user's own host name and this repo is public
(design.md D4): unset, there is no absolute URL to offer, so every check is
answered `{"message": "no bundle published", "kind": "up_to_date"}` and a
warning goes to the journal. Nothing else may appear in an offer: a `message` key
alongside them is read by the plugin as "there is nothing to do".

**Response, when no bundle is offered** — exactly these two keys:

```json
{"message": "no bundle published", "kind": "up_to_date"}
```

`kind` is not optional. `backgroundDownload()` routes a response to its
no-update handler only when `error` or `kind` is non-empty
(`CapacitorUpdaterPlugin.swift:4290`); a bare `{"message": …}` falls through to
`URL(string: res.url)` with an empty url, logs `Error no url or wrong format`
and counts a failed download — once per launch, on a healthy phone. The three
values it recognises are `up_to_date`, `blocked` and `failed`; anything else
normalises to `failed` (`normalizedUpdateResponseKind`, `:4106`), and `failed`
fires the `downloadFailed` listener.

The cases, in the order they are decided:

| Condition | `message` | `kind` |
|---|---|---|
| `app_id` is some other app | `unknown app` | `failed` |
| no manifest, or an unusable one | `no bundle published` | `up_to_date` |
| `version_name` is the published version | `up to date` | `up_to_date` |
| `version_build` < the manifest's `min_native` | `needs native 1.0.9` | `blocked` |
| the manifest names a zip that is not on disk | `bundle file missing` | `failed` |
| otherwise | *(an offer, see above)* | |

"No manifest" is `up_to_date`, not `failed`, and that is deliberate: an empty
bundles directory is this server's normal resting state — nothing is published
until it should be — and `failed` would log an error and fire `downloadFailed`
on every launch of an app with nothing wrong with it.

`min_native` compares the leading numeric parts (`1.0.12` → `(1, 0, 12)`). A
`version_build` that will not parse yields `()`, which sorts below everything,
so an unreadable shell is treated as too old and withheld from — the safe
direction.

### The status view

`GET /api/app/update` → `{"published": false, "bundles_dir": …}`, or
`{"published": true, "version", "checksum", "min_native", "published_at",
"notes", "url", "bytes", "available", "bundles_dir"}`. For `scripts/ship_web.sh`
and for a human; deliberately a different shape from the POST so it can never be
mistaken for an offer.

### The download

`GET /api/app/bundles/www-<version>.zip` → the zip, `application/zip`,
`Cache-Control: public, max-age=31536000, immutable` (a version's bytes never
change; publishing writes a new version). Any other filename is
`404 {"error":"not_found"}` — the name must match `www-<version>.zip` with
`<version>` in `[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}`, which spells no separator and
no `..`, and the resolved path's parent is re-checked against the bundles
directory so a symlink planted inside it cannot lead out.

The zip has `index.html` **at its root**, not under a `www/` directory.

### Bundles on disk

`server/bundles/` by default (`TASKMASTER_BUNDLES_DIR`), gitignored, written by
`scripts/publish_bundle.py`, which also maintains `manifest.json`:

```json
{"version": "2026.0904.1", "file": "www-2026.0904.1.zip",
 "checksum": "e4f331ef…", "bytes": 29943,
 "published": "2026-09-04T17:38:00Z", "notes": "…", "min_native": "1.0.9"}
```

`version` and `checksum` are re-validated on every read; an absent, unparseable
or invalid manifest reads as "no bundle published" rather than an error, so one
bad file cannot turn every launch into a failed check.

### Access — one gate here, not two

The **address allowlist applies in full**: these paths answer only loopback and
`100.64.0.0/10`, like everything else.

The **bearer token does not**, and that is not a choice. @capgo/capacitor-updater
8.51.15 has no way to send an `Authorization` header — its only request builder
sets `User-Agent`, `Accept` and `Content-Type` (`CapgoUpdater.swift:207`
`createRequest`) and there is no config key for headers. With `TASKMASTER_TOKEN`
set and these paths gated, every check would 401, the plugin would log
`getLatest failed` once per launch, and the symptom would be "live updates just
don't work" with nothing pointing at the token. So `app/middleware.py` exempts
`/api/app/update` and `/api/app/bundles/` from the token, and only the token.
What they expose to something already on the tailnet is a zip of
`www/index.html` — the same file already inside the app binary, no secrets in
it.

## Preferences, ordering, recurrence, categories, widget (round 5, 2026-09-04)

*Planner's contract. Server, client and widget build to this; whoever must
deviate edits this section first and says so.*

### Preferences — `GET /api/prefs`, `PUT /api/prefs`

One JSON document, stored by the server at `$TASKMASTER_PREFS`
(default `~/.config/taskmaster/prefs.json`, atomic writes). `PUT` replaces the
whole document (validated; unknown keys dropped; 422 names the field).
Defaults apply for anything missing:

```
{
  "categories": { "order": ["personal","work","claude","fun","inbox"], "hidden": [] },
  "chips":      { "order": ["p:personal","p:work","p:claude","p:fun","p:inbox","t:claude","t:alert"],
                  "hidden": [] },                 // "p:<category>" | "t:<tag>"; unknown ones are ignored
  "sort":       { "mode": "due" },                 // "due" | "priority" | "urgency" | "manual"
  "widget":     { "groups": ["overdue","today"],   // any of overdue,today,upcoming,none
                  "upcoming_days": 7,              // when "upcoming" is in groups
                  "category": null,                // null = all, else one category name
                  "rows": { "small": 3, "medium": 5, "large": 12 },
                  "show_category": false }
}
```

### Ordering moves to the server

`GET /api/tasks` (pending) now returns tasks **already in display order** and
each Task gains `"group": "overdue"|"today"|"upcoming"|"none"` (the client's
`groupOf()` rule, evaluated in the server's zone) and `"order": number|null`
(a Taskwarrior numeric UDA `order`, see below). Display order within a group:

- `sort.mode = "due"` (default): the canonical key from "Canonical order".
- `"priority"`: priority H>M>L>none first, then the canonical key.
- `"urgency"`: Taskwarrior `urgency` desc, then uuid.
- `"manual"`: tasks with `order` set first, ascending; the rest by the canonical key.
In every mode a non-null `order` wins inside its group when the mode is
`manual`; the other modes ignore it. Clients render in the order received and
only re-sort locally for optimistic rows (same rules, best effort).

`PATCH /api/tasks/{uuid}` accepts `"order": number|null`. Drag-to-reorder sends
the moved task a midpoint between its new neighbours' `order` values (the
client assigns 1000-spaced integers to a group the first time it is reordered,
one PATCH per task that changed). The UDA is declared in the user's `.taskrc`
(`uda.order.type=numeric`, `uda.order.label=Order`) — the server's install
script adds it if missing; tests declare it in their temp taskrc.

> **Added by the server, 2026-09-04 — `order` is refused when the UDA is not
> declared.** `PATCH {"order": …}` (a number *or* `null`) answers
> `409 {"error":"conflict"}` naming the two `.taskrc` lines, unless
> `task _get rc.uda.order.type` is non-empty. Verified on 3.4.2: with the UDA
> undeclared, `task <uuid> modify order:1500` exits **0** and sets the task's
> **description** to `"order:1500"` — the token is not an attribute Taskwarrior
> knows, so it falls through to the text — and `modify order:` sets it to
> `"order:"`. One drag would shred a screenful of real tasks, so the write is
> refused rather than attempted.
>
> An integral `order` comes back as an integer (`1500`, not `1500.0`).
> Taskwarrior is not consistent about which it exports for the same value, so
> the server normalises; a drag midpoint (`1250.5`) stays a float.

### Recurrence

Task gains `"until": "YYYY-MM-DD"|null`. `PATCH` accepts
`"recur": string|null` — one of `daily`, `weekdays`, `weekly`, `biweekly`,
`monthly`, `yearly`, or any Taskwarrior period Taskwarrior accepts — and
`"until": "YYYY-MM-DD"|null`. Semantics, all verified against the real `task`
in the tests:

- On a recurring **instance** (`parent` set): the change is applied to the
  parent template (`task <parent> modify recur:… until:…`); the response is the
  instance re-read. ~~(`recur` on instances mirrors the template)~~ — **it does
  not**, see the note below.
- On a plain pending task with a `due`: `recur:<value>` turns it into a
  template; Taskwarrior then spawns the first instance. The response is the
  **first instance** (status pending, `parent` set), and the client should
  replace the task it had with it. Without a `due` → 422 `invalid_request`
  ("recurrence needs a due date").
- `recur: null` on an instance = stop repeating: the parent template is
  deleted (`task <parent> delete`, confirmation off) and the current instance is
  kept as a plain task (~~`modify parent: recur: imask:` as far as Taskwarrior
  allows~~ — see below; the answer is "none of it"). ~~Other pending instances
  of that template are deleted with the template.~~ — they are not, and on
  `recurrence.limit=1` there is only ever one.

> **Server's findings on Taskwarrior 3.4.2, 2026-09-04.** All of this was run
> against the real binary in a throwaway data dir and is pinned by
> `server/tests/test_recurrence.py`; the tests fail if a Taskwarrior upgrade
> changes any of it.
>
> **1. `recur`/`until` on an instance are a snapshot, not a mirror.** After
> `task <template> modify recur:weekly until:2026-12-01`, the pending instance
> still exports `recur:daily` and no `until` at all. Reading them off the
> instance would show the user the schedule they had just replaced, so **the
> server reports `recur` and `until` from the parent template** (one extra
> `status:recurring` export per response, and only when some row has a
> `parent`).
>
> **2. Stopping cannot be spelled on the instance.**
> - `task <instance> modify recur:` → exit 2, *"You cannot remove the recurrence
>   from a recurring task."* `task import` with the field stripped out hits the
>   same check and is refused too.
> - `task <instance> modify parent:` → exit 0, and it is **harmful**: the
>   instance is promoted to a live `status:recurring` template and immediately
>   spawns a *fresh* instance. The row the user asked to keep leaves the pending
>   list; the series they asked to stop carries on.
> - `task <instance> modify imask:` → exit 0, and changes nothing that matters.
>
> So **"stop repeating" is `task <parent> delete` and nothing else.** That is
> enough: the surviving instance never spawns another (completing it produces
> nothing — verified), which is the behaviour the user asked for.
>
> **3. What remains, and what the API does about it.** In Taskwarrior's own
> records the stopped instance keeps `recur:daily` and a `parent` pointing at
> the now-deleted template, permanently. Since that state is unreachable and
> means nothing, **the server reports an instance whose template is gone as a
> plain task**: `recur`, `until` and `parent` all come back `null`. Without
> that, "stop repeating" would appear to do nothing — the row would say it
> repeats forever. A terminal `task <uuid> info` still shows the residue.
>
> **4. Deleting the template leaves other pending instances alone** (exit 0,
> instance untouched). With `recurrence.limit=1` — the default, and what this
> box runs — Taskwarrior only ever materialises one instance ahead, so in
> practice there is exactly one and it is the one being kept.
>
> **5. Starting again on a stopped task needs the parent cleared.**
> `modify recur:weekly` on a task that still carries a dead `parent` writes the
> field and spawns nothing, because a task with a `parent` is an instance, not
> a template. So the promotion path sends `parent: imask: recur:<value>`
> together, and that does produce a template and an instance. The response is
> the new instance, exactly as for a plain task.
>
> **6. `recur` without a `due`** exits 2 (*"You cannot specify a recurring task
> without a due date."*). The server checks first and answers the `422` the
> contract names. A `due` sent in the same PATCH counts.
>
> **7. An unsupported period** (`recur:bogusly`) exits 2 with *"The duration
> value 'bogusly' is not supported."*, which surfaces as the usual
> `502 task_failed` carrying the CLI's own words — the contract's "any
> Taskwarrior period Taskwarrior accepts" is enforced by Taskwarrior. The
> server only checks the token is `^[A-Za-z0-9]{1,20}$` before it reaches argv.
>
> **Periods are case-sensitive in both directions and the server folds
> neither.** `P1M` and `P10D` (ISO-8601) are accepted while `p1m` is not;
> `weekly`, `daily`, `weekdays`, `biweekly`, `monthly`, `yearly`, `3days` and
> `2w` are accepted while `Weekly` is not. Lower-casing would break the ISO
> forms and upper-casing would break the named ones, so the string is passed
> through exactly as sent. Clients should send the lower-case named periods.
>
> **9. `until` in the past** is accepted and does what it says: Taskwarrior
> spawns the instance and then reaps it, so the `200` describes a task that is
> gone by the next `GET /api/tasks`. Not guarded — "repeat until yesterday" is
> a coherent thing to have asked for — and the client refreshes after every
> write anyway (design.md D6), so the row does not linger.
>
> **8. `recur: null` addressed at a template itself** (not something the phone
> can reach — templates are in no list) deletes that template.

### Categories

`GET /api/meta` gains `"categories": [{name, count, hidden}]` in prefs order
(hidden ones included, flagged), followed by in-use categories not in the
order list. `count` is **pending** tasks — it is the number the filter chip
shows (design.md D9) — so a category only completed tasks use is listed with
`count: 0` rather than dropped. `projects` is unchanged, for the round-4
clients still reading it.

`POST /api/categories/rename` `{"from","to"}` → `204`
(~~`task project:<from> modify project:<to>`~~ — see below — over all
non-deleted tasks, bulk confirmation off; prefs order/hidden renamed too).
`POST /api/categories/delete` `{"name", "move_to": string|null}` → `204`
(tasks move to `move_to` or lose their category; removed from prefs).
Names match `^[A-Za-z0-9_.-]{1,40}$`. `move_to` equal to `name` is `422`.

> **Server's findings, 2026-09-04.** The command in the line above is not safe
> as written; the server runs
> `task rc.bulk=0 status.not:deleted project.is:<from> modify project:<to>`.
> Three reasons, all verified on 3.4.2:
>
> - **`project.is:`, not `project:`.** Taskwarrior attribute filters are
>   **prefix matches**: `task project:work modify project:job` also renames
>   `workshop` and `work.sub`. On the real database that is silent data loss.
> - **`rc.bulk=0`.** `rc.confirmation=off` does *not* cover the bulk prompt.
>   Over five tasks the modify asks *"Modify task 1 …? (yes/no/all/quit)"*,
>   reads EOF, prints "Task not modified." and exits **1** having changed
>   nothing — with the server's usual flags and no other clue than the exit
>   code.
> - **Count first.** With no matching task the modify exits 1 with empty
>   output, which would surface as a `502` whose `detail` is the empty string.
>   Renaming a category nobody has used yet is not an error — the prefs rename
>   is the point of the call — so a match count decides whether to run it.
>
> Renaming also moves the `p:<name>` filter chips and `widget.category`; a
> chip or a widget filter pointing at a category that no longer exists shows an
> empty screen with nothing to explain it. Deleting removes them (the widget
> filter follows `move_to`, or clears).

### Widget feed — `GET /api/widget`

The server applies `prefs.widget` and returns what the widget draws, nothing
else: `{"updated": iso, "total": int, "rows": [{uuid, text, due, overdue,
group, category}]}` — `due` is the label ("overdue" | "today" | "2:30 pm" |
"Thu Sep 4" | ""), `text` is the description flattened to one line, rows are
already in display order and limited to `prefs.widget.rows.large` (the widget
truncates further per family using the caps, which the response echoes as
`"caps": {"small": 3, "medium": 5, "large": 12}` — a separate key, since
`rows` is the array). Same allowlist/token rules as
`/api/tasks`. The widget stops sorting or classifying anything.

`category` is the row's category **only when `prefs.widget.show_category` is
true**, and `""` otherwise: the widget draws it whenever the string is
non-empty and has no other way to read the pref. An uncategorised task is `""`
either way.

`total` is the count **behind** the `rows.large` cap — what "+N more" counts up
to. `updated` is the server's clock, local with offset.

The `upcoming` window is a **date** comparison, like everything else that
touches `due` (design.md D3): `upcoming_days: 7` means "on or before the date
seven days from today", so a task due at 09:00 on the seventh day is in for the
whole of that day rather than falling out at lunchtime. It bounds only the
`upcoming` group; `overdue`, `today` and `none` are unaffected by it.

Labels in full, matching the client's `dueLabel()`: `""` (no due), `"overdue"`,
`"today"` (date-only today), `"2:30 pm"` (clocked today, still to come),
`"Tomorrow"` / `"Tomorrow · 2:30 pm"`, `"Thu Sep 10"` (date-only, weekday
carried), `"Sep 12 · 2:30 pm"` (clocked, weekday dropped so the row stays one
line), and `"Sat Jan 2, 2027"` once the year differs.

### Widget grouping by category (2026-09-04, round 6)

`prefs.widget.group_by`: `"due"` (default) | `"category"`. `GET /api/widget`
echoes it as top-level `"group_by"`. With `"category"`, rows are ordered by
category — `prefs.categories.order` first, then other categories
alphabetically (case-folded, raw name as tie-break), tasks with no category last under the name `""` — and within a
category in the normal display order; every row carries `category` regardless
of `show_category`. The widget draws a small header before the first row of
each category run (no-category rows get the header "No category") and counts
each header as roughly half a row against the family's cap. With `"due"`
nothing changes from round 5.
