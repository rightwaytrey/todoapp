"""GET /health — unprefixed and outside the token gate (docs/api.md Health).

This is what deploy/install.sh curls and what the app's Settings screen calls
"Test connection", so it has to answer even when the token is wrong and even
when Taskwarrior itself is broken: a 200 with `ok: false` tells the user *why*,
where a 502 would only tell them "something".
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from .. import taskwarrior as tw
from ..config import settings
from ..serialize import now_iso

log = logging.getLogger("taskmaster.health")

router = APIRouter(tags=["health"])


async def health_body() -> dict:
    body = {
        "ok": True,
        "version": settings.version,
        "time": now_iso(),
        "tz": settings.tz_name,
        "task_version": None,
        "pending": None,
    }
    try:
        body["task_version"] = await tw.version()
        body["pending"] = len(await tw.export("status:pending"))
    except Exception as exc:                                     # noqa: BLE001
        # Any failure to reach `task` at all is a health answer, not an error.
        log.warning("health: task unavailable: %s", exc)
        body["ok"] = False
        body["task_version"] = None
        body["detail"] = getattr(exc, "detail", None) or "%s: %s" % (
            type(exc).__name__, exc)
    return body


@router.get("/health")
async def health():
    return await health_body()
