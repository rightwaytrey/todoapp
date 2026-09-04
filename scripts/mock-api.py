#!/usr/bin/env python3
"""In-memory stand-in for the TaskMaster API (docs/api.md), for UI work.

Why this exists: www/index.html is the whole client and it is worth being able
to develop and screenshot it in a desktop browser without Taskwarrior, without
the real server on :8101, and without a network at all. This implements enough
of the contract to drive every screen — every endpoint the client calls, the
same error envelope, the same ETag/304 dance, the same CORS preflight — over
a dict in memory that resets when you stop it.

    python3 scripts/mock-api.py --port 8111
    python3 -m http.server 8112 -d www
    open http://localhost:8112/?server=http://localhost:8111

`--latency-ms N` delays every response by N ms. That is not padding: the whole
point of the client's optimistic write path (design.md D6) is that the screen
moves before the server answers, and with a 0 ms loopback server you cannot
see whether it does. Run with `--latency-ms 1500` and watch a completed task
strike out instantly while the request is still in flight.

stdlib only. Not a security boundary, not persistent, not the contract — the
contract is docs/api.md, and where this file disagrees with it, it is wrong.
"""

import argparse
import hashlib
import json
import re
import sys
import threading
import time
import uuid as uuidlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0-mock"
TZ_NAME = "America/Chicago"
TASK_VERSION = "3.4.2"

# The real server strips these on the way out and ignores them on the way in;
# they are `pa retag`'s maintenance tags for the old phone filter (api.md).
RESERVED_TAGS = {"today", "overdue", "due"}

PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
DUE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DUE_CLOCK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")

# The `pa` projects, always present and in this order, then anything else.
PA_PROJECTS = ["personal", "work", "claude", "fun", "inbox"]

LOCK = threading.Lock()
TASKS = {}          # uuid -> task dict
LATENCY_MS = 0


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------
# The mock runs in the host's local zone and pretends that is America/Chicago,
# which on this box it is. Everything the client cares about (`due`) is a local
# wall-clock string anyway, so there is no conversion to get wrong here.

def now():
    return datetime.now().astimezone()


def iso(dt):
    return dt.isoformat(timespec="seconds")


def local_date(offset_days=0):
    return (now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def due_at_for(due):
    """The same instant as `due`, as an ISO string with offset."""
    if not due:
        return None
    fmt = "%Y-%m-%dT%H:%M" if "T" in due else "%Y-%m-%d"
    naive = datetime.strptime(due, fmt)
    return iso(naive.astimezone())


def valid_due(v):
    if not isinstance(v, str):
        return False
    if DUE_DATE_RE.match(v):
        return True
    if DUE_CLOCK_RE.match(v):
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M")
            return True
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def make_task(description, **kw):
    t = {
        "uuid": str(uuidlib.uuid4()),
        "description": description,
        "status": "pending",
        "project": None,
        "priority": None,
        "due": None,
        "due_at": None,
        "tags": [],
        "annotations": [],
        "recur": None,
        "parent": None,
        "depends": [],
        "blocked": False,
        "urgency": 1.0,
        "entry": iso(now() - timedelta(days=3)),
        "modified": iso(now()),
        "end": None,
    }
    t.update(kw)
    t["due_at"] = due_at_for(t["due"])
    return t


def seed():
    """~14 pending covering every group, plus 3 completed.

    Dates are computed from the clock at startup, so the four groups are
    always populated whatever day you run this.

    Three things the seed is deliberately shaped to show:

    * the canonical order (api.md, design.md D8) -- the Today group holds one
      clocked task and four date-only ones at H / M / L / none, so "clocked
      before date-only" and "H > M > L > none" are both visible in one group
      without reading any code;
    * the filter chips (D9) -- `personal` and `work` both carry several tasks,
      and `claude` is BOTH a project and a tag on two different tasks, which is
      the collision the chip values are namespaced (`p:` / `t:`) to survive;
    * a button task -- one pending task tagged `button`, which the client
      renders as a confirm-first action row instead of a tappable circle.
    """
    n = now()
    # An overdue *clocked* task has to be in the past but still today where it
    # can be, because "today, but the clock time has passed" is its own branch
    # in the client's grouping and is the one most likely to be got wrong.
    if n.hour >= 2:
        overdue_clock = (n - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    else:
        overdue_clock = local_date(-1) + "T14:30"
    # Conversely a today-clocked task must stay in the future or it lands in
    # Overdue and the Today group loses its clocked example.
    later = n + timedelta(hours=2)
    if later.strftime("%Y-%m-%d") != n.strftime("%Y-%m-%d"):
        today_clock = local_date(0) + "T23:45"
    else:
        today_clock = later.strftime("%Y-%m-%dT%H:%M")

    plants_parent = str(uuidlib.uuid4())

    rows = [
        # --- overdue (2, one of them clocked) --------------------------------
        make_task("renew the car tabs before they lapse",
                  project="personal", priority="H", due=local_date(-4),
                  urgency=15.2),
        make_task("call the dentist back about the crown",
                  project="personal", priority="M", due=overdue_clock,
                  tags=["alert"], urgency=13.9),
        # --- today (5, one of them clocked) ----------------------------------
        # The clocked one sorts FIRST of these whatever its priority, then the
        # date-only four by H, M, L, none: that is the whole of the canonical
        # order's second and fourth terms, on screen, in one group.
        make_task("water the plants", project="personal", due=local_date(0),
                  recur="daily", parent=plants_parent, urgency=11.4),
        make_task("stand-up with the platform team", project="work",
                  priority="M", due=today_clock, urgency=12.1),
        make_task("review the release checklist", project="work",
                  priority="H", due=local_date(0), urgency=12.6),
        make_task("reply to the vendor about the invoice", project="work",
                  priority="L", due=local_date(0), tags=["claude"],
                  urgency=8.1),
        # A button task: an upstream stages one of these so the phone has a
        # one-tap way to trigger something, and completing it acts immediately
        # downstream. The `button` tag is the whole signal -- the client keys
        # off it alone to render a confirm-first action row instead of a
        # tappable circle (design.md D9), so the shape here matters more than
        # the words: pending, tagged `button`, project, date-only due.
        make_task("Skip today's workout (tap only if you really did)",
                  project="personal", due=local_date(0), tags=["button"],
                  urgency=10.84),
        # --- upcoming (3) -----------------------------------------------------
        make_task("write the quarterly summary", project="work", priority="M",
                  due=local_date(2), urgency=9.8,
                  annotations=[{"entry": iso(n - timedelta(days=1)),
                                "text": "outline is in ~/notes/q3.md"}]),
        make_task("look into why the widget feed lags", project="claude",
                  due=local_date(4), tags=["claude"], urgency=8.6),
        make_task("book the campsite for October", project="fun",
                  priority="L", due=local_date(10) + "T14:30", urgency=7.2),
        # --- no date (3) ------------------------------------------------------
        make_task("read the Taskwarrior 3.x migration notes", project="inbox",
                  urgency=3.1),
        make_task("sort out the garage shelving", project="personal",
                  priority="L", urgency=2.4),
        make_task("replace the kitchen tap washer", project="personal",
                  urgency=1.8),
    ]

    # One blocked task: it depends on the tap washer above, which is pending.
    washer = rows[-1]
    blocked = make_task("fix the drip under the sink", project="personal",
                        priority="M", due=local_date(3),
                        depends=[washer["uuid"]], blocked=True, urgency=8.0)
    rows.append(blocked)

    done_rows = [
        make_task("pay the water bill", project="personal", status="completed",
                  due=local_date(0), end=iso(n - timedelta(hours=3)),
                  urgency=0.0),
        make_task("email the tax people back", project="work",
                  status="completed", priority="H",
                  end=iso(n - timedelta(days=1, hours=5)), urgency=0.0),
        make_task("water the plants", project="personal", status="completed",
                  due=local_date(-2), recur="daily", parent=plants_parent,
                  end=iso(n - timedelta(days=2, hours=9)), urgency=0.0),
    ]

    for t in rows + done_rows:
        TASKS[t["uuid"]] = t


# ---------------------------------------------------------------------------
# request handling
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def public(t):
    """A copy with the reserved maintenance tags stripped, as the real one does."""
    c = dict(t)
    c["tags"] = [x for x in t.get("tags", []) if x not in RESERVED_TAGS]
    c["annotations"] = [dict(a) for a in t.get("annotations", [])]
    return c


def list_tasks(status):
    with LOCK:
        rows = list(TASKS.values())
    if status == "completed":
        cutoff = now() - timedelta(days=30)
        out = [t for t in rows
               if t["status"] == "completed"
               and t["end"] and datetime.fromisoformat(t["end"]) >= cutoff]
        out.sort(key=lambda t: t["end"], reverse=True)
        return [public(t) for t in out[:200]]
    pending = [t for t in rows if t["status"] == "pending"]
    pending.sort(key=lambda t: t["urgency"], reverse=True)
    if status == "all":
        comp = [t for t in rows if t["status"] == "completed"]
        comp.sort(key=lambda t: t["end"] or "", reverse=True)
        return [public(t) for t in pending + comp]
    return [public(t) for t in pending]


def clean_tags(v, field="tags"):
    if v is None:
        return []
    if not isinstance(v, list):
        raise ApiError(422, "invalid_request", "%s must be a list of strings" % field)
    out = []
    for x in v:
        if not isinstance(x, str) or not TAG_RE.match(x):
            raise ApiError(422, "invalid_request",
                           "%s entries must match ^[A-Za-z0-9_]{1,40}$" % field)
        if x in RESERVED_TAGS:
            continue          # dropped silently, per the contract
        if x not in out:
            out.append(x)
    return out


def apply_fields(t, body, creating):
    """Shared create/patch validation. On PATCH an absent key means untouched,
    an explicit null clears."""
    if "description" in body or creating:
        d = body.get("description")
        if creating and (not isinstance(d, str) or not d.strip()):
            raise ApiError(422, "invalid_request", "description is required")
        if "description" in body:
            if not isinstance(d, str) or not d.strip():
                raise ApiError(422, "invalid_request",
                               "description must be a non-empty string")
            t["description"] = d.strip()

    if "project" in body:
        p = body["project"]
        if p is None:
            t["project"] = None
        elif isinstance(p, str) and PROJECT_RE.match(p):
            t["project"] = p
        else:
            raise ApiError(422, "invalid_request",
                           "project must match ^[A-Za-z0-9_.-]{1,40}$ or be null")

    if "priority" in body:
        p = body["priority"]
        if p is None:
            t["priority"] = None
        elif p in ("H", "M", "L"):
            t["priority"] = p
        else:
            raise ApiError(422, "invalid_request",
                           "priority must be one of H, M, L or null")

    if "due" in body:
        d = body["due"]
        if d is None:
            t["due"] = None
            t["due_at"] = None
        elif valid_due(d):
            t["due"] = d
            t["due_at"] = due_at_for(d)
        else:
            raise ApiError(422, "invalid_request",
                           "due must be YYYY-MM-DD or YYYY-MM-DDTHH:MM or null")

    if "tags" in body:
        keep = [x for x in t.get("tags", []) if x in RESERVED_TAGS]
        t["tags"] = keep + clean_tags(body["tags"])

    t["modified"] = iso(now())
    return t


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "taskmaster-mock/" + VERSION

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, If-None-Match")
        self.send_header("Access-Control-Expose-Headers", "ETag")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status, payload, etag=None):
        if LATENCY_MS:
            time.sleep(LATENCY_MS / 1000.0)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        if etag:
            self.send_header("ETag", etag)
        if payload is None:
            self.send_header("Content-Length", "0")
        else:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, code, detail):
        self._send(status, {"error": code, "detail": detail})

    def _read_body(self):
        """Consume the whole request body before anything else can fail.

        HTTP/1.1 keep-alive plus an unread body is a connection-corrupting
        combination: the leftover bytes get parsed as the next request line,
        and the client sees a 501 on a request it never made. That is not
        hypothetical — it happened on the 404 path, where the uuid lookup
        raised before the PATCH body was ever read."""
        n = int(self.headers.get("Content-Length") or 0)
        self._raw = self.rfile.read(n) if n else b""

    def _body(self):
        if not self._raw:
            return {}
        try:
            v = json.loads(self._raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "bad_request", "body is not valid JSON")
        if not isinstance(v, dict):
            raise ApiError(400, "bad_request", "body must be a JSON object")
        return v

    # -- verbs -------------------------------------------------------------
    def do_OPTIONS(self):
        self._read_body()
        # The preflight the browser sends before any PATCH/DELETE or any
        # request carrying Authorization. Without this the client's writes
        # fail in the dev browser and look exactly like "the server is down".
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            self._read_body()
            self._route(method)
        except ApiError as e:
            self._error(e.status, e.code, e.detail)
        except Exception as e:                       # noqa: BLE001
            self._error(500, "task_failed", "mock blew up: %r" % (e,))

    def _route(self, method):
        path, _, query = self.path.partition("?")
        path = path.rstrip("/") or "/"
        params = {}
        for pair in query.split("&"):
            if not pair:
                continue
            k, _, v = pair.partition("=")
            params[k] = v

        if path == "/health" and method == "GET":
            with LOCK:
                pending = sum(1 for t in TASKS.values() if t["status"] == "pending")
            return self._send(200, {
                "ok": True, "version": VERSION, "time": iso(now()),
                "tz": TZ_NAME, "task_version": TASK_VERSION, "pending": pending,
            })

        if path == "/api/meta" and method == "GET":
            with LOCK:
                rows = list(TASKS.values())
            others = sorted({t["project"] for t in rows
                             if t["project"] and t["project"] not in PA_PROJECTS})
            tags = sorted({x for t in rows if t["status"] == "pending"
                           for x in t["tags"] if x not in RESERVED_TAGS})
            return self._send(200, {
                "projects": PA_PROJECTS + others, "tags": tags,
                "priorities": ["H", "M", "L"], "tz": TZ_NAME, "now": iso(now()),
            })

        if path == "/api/tasks" and method == "GET":
            status = params.get("status") or "pending"
            if status not in ("pending", "completed", "all"):
                raise ApiError(422, "invalid_request",
                               "status must be pending, completed or all")
            data = list_tasks(status)
            body = json.dumps(data).encode("utf-8")
            etag = '"%s"' % hashlib.md5(body).hexdigest()[:16]
            if self.headers.get("If-None-Match") == etag:
                # 304 is what makes the client's 30 s poll cheap.
                if LATENCY_MS:
                    time.sleep(LATENCY_MS / 1000.0)
                self.send_response(304)
                self._cors()
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, data, etag=etag)

        if path == "/api/tasks" and method == "POST":
            body = self._body()
            t = make_task("")
            apply_fields(t, body, creating=True)
            t["entry"] = iso(now())
            t["urgency"] = round(5.0 + (2.0 if t["due"] else 0.0) +
                                 {"H": 6.0, "M": 3.9, "L": 1.8}.get(t["priority"], 0.0), 2)
            with LOCK:
                TASKS[t["uuid"]] = t
            return self._send(201, public(t))

        # A full 36-character uuid only. The real server refuses anything
        # else before it can reach an argv slot, because the value becomes a
        # Taskwarrior *filter* and a partial one prefix-matches (api.md).
        m = re.match(r"^/api/tasks/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(/[a-z]+)?$", path)
        if m:
            uid, sub = m.group(1), (m.group(2) or "")
            with LOCK:
                t = TASKS.get(uid)
            if t is None:
                raise ApiError(404, "not_found", "no task with that uuid")

            if sub == "" and method == "GET":
                return self._send(200, public(t))

            if sub == "" and method == "PATCH":
                with LOCK:
                    apply_fields(t, self._body(), creating=False)
                return self._send(200, public(t))

            if sub == "" and method == "DELETE":
                # Taskwarrior keeps the record (and stamps `end`); so do we.
                # Idempotent: a second DELETE is another 204.
                with LOCK:
                    if t["status"] != "deleted":
                        t["status"] = "deleted"
                        t["end"] = iso(now())
                        t["modified"] = t["end"]
                return self._send(204, None)

            if sub == "/done" and method == "POST":
                with LOCK:
                    # Idempotent (api.md): a done replayed from the client's
                    # offline queue must settle, not raise — and must not
                    # spawn a second recurrence.
                    if t["status"] == "completed":
                        return self._send(200, public(t))
                    t["status"] = "completed"
                    t["end"] = iso(now())
                    t["modified"] = t["end"]
                    spawn = None
                    if t["parent"]:
                        # Taskwarrior spawns the next instance itself; the
                        # client is supposed to just see it on the next list,
                        # so the mock had better produce one.
                        spawn = dict(t)
                        spawn["uuid"] = str(uuidlib.uuid4())
                        spawn["status"] = "pending"
                        spawn["end"] = None
                        spawn["annotations"] = []
                        if t["due"]:
                            base = t["due"][:10]
                            nxt = (datetime.strptime(base, "%Y-%m-%d") +
                                   timedelta(days=1)).strftime("%Y-%m-%d")
                            spawn["due"] = nxt + t["due"][10:]
                            spawn["due_at"] = due_at_for(spawn["due"])
                        TASKS[spawn["uuid"]] = spawn
                return self._send(200, public(t))

            if sub == "/undone" and method == "POST":
                with LOCK:
                    if t["status"] != "completed":
                        raise ApiError(409, "conflict", "task is not completed")
                    t["status"] = "pending"
                    t["end"] = None
                    t["modified"] = iso(now())
                return self._send(200, public(t))

            if sub == "/annotations" and method == "POST":
                body = self._body()
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ApiError(422, "invalid_request",
                                   "text must be a non-empty string")
                with LOCK:
                    t["annotations"].append({"entry": iso(now()),
                                             "text": text.strip()})
                    t["modified"] = iso(now())
                return self._send(200, public(t))

            raise ApiError(404, "not_found", "no such endpoint")

        raise ApiError(404, "not_found", "no such endpoint")


def main():
    global LATENCY_MS
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--latency-ms", type=int, default=0,
                    help="delay every response by N ms so the optimistic "
                         "write path is visible to the naked eye")
    args = ap.parse_args()
    LATENCY_MS = max(0, args.latency_ms)

    seed()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        "taskmaster mock on http://%s:%d  (%d tasks seeded, latency %d ms)\n"
        % (args.host, args.port, len(TASKS), LATENCY_MS))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
