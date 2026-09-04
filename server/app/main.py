"""TaskMaster API — a thin HTTP layer over the `task` CLI (docs/api.md).

Base path /api, plus an unprefixed /health. No database, no sessions, no
background loops: every request is one or two `task` invocations and the answer.
Taskwarrior is the store (docs/design.md D1), which is why the whole server fits
in eight files.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .errors import TaskFailed
from .middleware import AccessControl
from .routers import app_update, health, meta, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("taskmaster")

ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden",
               404: "not_found", 405: "method_not_allowed", 409: "conflict",
               422: "invalid_request"}

app = FastAPI(title=settings.app_name, version=settings.version,
              docs_url=None, redoc_url=None)

# Added first, so it ends up *inside* AccessControl: Starlette applies user
# middleware outermost-last-added. The app runs at capacitor://localhost on the
# phone and http://localhost:* in the dev browser, so any origin is allowed —
# the address allowlist, not the origin, is what actually keeps strangers out
# (docs/api.md Access).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "If-None-Match"],
    expose_headers=["ETag"],
)
app.add_middleware(AccessControl, settings=settings)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        body = detail
    else:
        body = {"error": ERROR_CODES.get(exc.status_code, "error"),
                "detail": str(detail)}
    return JSONResponse(body, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(p) for p in first.get("loc", ())[1:])
    msg = first.get("msg", "invalid")
    # pydantic prefixes its own message with "Value error, "; the field name is
    # what docs/api.md promises, so keep that and drop the noise.
    if msg.startswith("Value error, "):
        msg = msg[len("Value error, "):]
    return JSONResponse(
        {"error": "invalid_request",
         "detail": "%s: %s" % (where, msg) if where else msg},
        status_code=422)


@app.exception_handler(TaskFailed)
async def task_failed(request: Request, exc: TaskFailed):
    """502 task_failed — the `task` CLI said no. `detail` is its own words."""
    log.warning("task_failed on %s %s: %s", request.method, request.url.path,
                exc.detail)
    return JSONResponse({"error": "task_failed", "detail": exc.detail},
                        status_code=502)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"error": "server_error", "detail": "%s: %s" % (type(exc).__name__, exc)},
        status_code=500)


app.include_router(health.router)                                # /health
app.include_router(meta.router, prefix=settings.api_prefix)      # /api/meta
app.include_router(tasks.router, prefix=settings.api_prefix)     # /api/tasks
# /api/app/update + /api/app/bundles/… — the shell's own live-update endpoints
# (docs/design.md D11). Nothing to do with tasks; the phone's WebView never
# calls these, the native plugin does.
app.include_router(app_update.router, prefix=settings.api_prefix)

log.info("taskmaster %s up: task=%s tz=%s auth=%s allow=%s bundles=%s",
         settings.version, settings.task_bin, settings.tz_name,
         "token" if settings.token else "none",
         ",".join(settings.allow_cidrs), settings.bundles_dir)
