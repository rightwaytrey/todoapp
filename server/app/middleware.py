"""Who is allowed to talk to this server (docs/api.md "Access").

Written as **pure ASGI** rather than a FastAPI dependency or a
`BaseHTTPMiddleware` for one reason: docs/api.md says the address check happens
"before anything else runs". A dependency runs after routing, body parsing and
validation; this runs before the request has been looked at at all, so a
stranger cannot even provoke a 422 out of the thing.

It is installed **outermost**, outside CORSMiddleware, so nothing — not even a
preflight — is answered before the address has been checked. The cost of that
ordering is that its own 403/401 would carry no CORS headers and a browser would
see an opaque network error instead of the reason, so the two rejections add
`access-control-allow-origin: *` themselves.
"""
from __future__ import annotations

import ipaddress
import json
import logging
from typing import Optional

from .config import Settings

log = logging.getLogger("taskmaster.access")

# /health is deliberately outside the token gate (docs/api.md Health): it is
# what deploy/install.sh and `systemctl` checks curl, and it leaks nothing but
# a pending count. The address allowlist still applies to it.
#
# So are the two app-update paths, and that one is not a choice: @capgo/
# capacitor-updater 8.51.15 has **no way to send an Authorization header**. Its
# only request builder sets User-Agent, Accept and Content-Type and nothing else
# (node_modules/@capgo/capacitor-updater/ios/Sources/CapacitorUpdaterPlugin/
# CapgoUpdater.swift:207 createRequest), and there is no config key for headers.
# With a token configured and these paths gated, every check would 401, the
# plugin would log "getLatest failed" once per launch, and the symptom would be
# "live updates just don't work" with nothing pointing at the token. Better to
# say plainly which two paths the token does not cover.
#
# What they expose is a zip of www/index.html — the same file already inside the
# app binary, no secrets in it — to a caller that has already passed the address
# allowlist, i.e. something on the tailnet. The allowlist is the real gate
# (docs/design.md D4); this is the one place the second gate cannot follow.
OPEN_PATHS = frozenset({"/health"})
OPEN_PREFIXES = ("/api/app/update", "/api/app/bundles/")


def _token_exempt(path: Optional[str]) -> bool:
    return path in OPEN_PATHS or (path or "").startswith(OPEN_PREFIXES)


def _norm(host: str) -> Optional[ipaddress._BaseAddress]:
    """Parse a client address, unwrapping the ::ffff:127.0.0.1 form.

    uvicorn hands back whatever the socket says. On a dual-stack listener an
    IPv4 peer arrives as an IPv4-mapped IPv6 address, which would not match
    127.0.0.0/8 or 100.64.0.0/10 unless it is unmapped first.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def is_allowed(host: Optional[str], settings: Settings) -> bool:
    if not host:
        # No peer address (a unix socket, or a transport that omits it). Deny:
        # this middleware is the only gate when no token is configured, and a
        # gate that fails open is not a gate.
        return False
    ip = _norm(host)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    return any(ip in net for net in settings.networks)


class AccessControl:
    """Address allowlist + the optional bearer token."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        client = scope.get("client")
        host = client[0] if client else None
        if not is_allowed(host, self.settings):
            log.warning("refused %s %s from %s", scope.get("method"),
                        scope.get("path"), host)
            return await self._reject(send, 403, "forbidden",
                                      "This address is not on the tailnet.")

        token = self.settings.token
        if token and not _token_exempt(scope.get("path")) \
                and scope.get("method") != "OPTIONS":
            # OPTIONS is exempt because a CORS preflight never carries
            # Authorization; the real request that follows it does.
            if not self._bearer_ok(scope, token):
                return await self._reject(send, 401, "unauthorized",
                                          "Missing or wrong bearer token.")

        return await self.app(scope, receive, send)

    @staticmethod
    def _bearer_ok(scope, token: str) -> bool:
        for key, value in scope.get("headers") or ():
            if key == b"authorization":
                got = value.decode("latin-1").strip()
                if got.lower().startswith("bearer "):
                    return got[7:].strip() == token
                return False
        return False

    @staticmethod
    async def _reject(send, status: int, code: str, detail: str) -> None:
        body = json.dumps({"error": code, "detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"access-control-allow-origin", b"*"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
