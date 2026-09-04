"""The address allowlist and the optional bearer token (docs/api.md Access)."""
from __future__ import annotations

import pytest

from app.config import reload_settings, settings

from .conftest import client_from, make

# Example addresses inside Tailscale's 100.64.0.0/10, not this tailnet's real
# ones: what is under test is the range, and a real address would put the user's
# network in a public repo.
TAILNET = "100.64.0.10"            # a box on the tailnet
PHONE = "100.100.100.100"          # the phone
OFF_NET = "192.168.1.5"            # the LAN: close, but not the tailnet


async def test_loopback_is_allowed(client):
    assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize("host", [TAILNET, PHONE, "100.64.0.1", "100.127.255.254"])
async def test_tailnet_addresses_are_allowed(host):
    async with client_from(host) as c:
        assert (await c.get("/api/tasks")).status_code == 200


@pytest.mark.parametrize("host", [OFF_NET, "10.0.0.9", "8.8.8.8",
                                  "100.63.255.255", "100.128.0.1"])
async def test_everything_else_is_403(host):
    async with client_from(host) as c:
        r = await c.get("/api/tasks")
        assert r.status_code == 403
        assert r.json()["error"] == "forbidden"


async def test_403_applies_to_health_too(client):
    async with client_from(OFF_NET) as c:
        assert (await c.get("/health")).status_code == 403


async def test_403_happens_before_routing(client):
    """Nothing downstream runs, so an off-net client cannot even provoke a 404."""
    async with client_from(OFF_NET) as c:
        r = await c.get("/api/nothing-here")
        assert r.status_code == 403


async def test_403_carries_cors_so_a_browser_can_read_it(client):
    async with client_from(OFF_NET) as c:
        r = await c.get("/api/tasks")
        assert r.headers["access-control-allow-origin"] == "*"


async def test_ipv6_mapped_loopback_is_allowed():
    async with client_from("::ffff:127.0.0.1") as c:
        assert (await c.get("/health")).status_code == 200


async def test_ipv6_loopback_is_allowed():
    async with client_from("::1") as c:
        assert (await c.get("/health")).status_code == 200


async def test_extra_cidrs_from_the_environment(monkeypatch):
    monkeypatch.setenv("TASKMASTER_ALLOW_CIDRS", "192.168.1.0/24")
    reload_settings()
    try:
        async with client_from(OFF_NET) as c:
            assert (await c.get("/api/tasks")).status_code == 200
    finally:
        monkeypatch.delenv("TASKMASTER_ALLOW_CIDRS")
        reload_settings()


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
async def test_no_token_configured_means_no_auth(client):
    assert settings.token is None
    assert (await client.get("/api/tasks")).status_code == 200


async def test_token_gate(client, monkeypatch):
    monkeypatch.setenv("TASKMASTER_TOKEN", "s3cret")
    reload_settings()
    try:
        assert (await client.get("/api/tasks")).status_code == 401
        r = await client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"

        r = await client.get("/api/tasks",
                             headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200

        # /health stays open — it is what install.sh and "Test connection" curl.
        assert (await client.get("/health")).status_code == 200

        # A preflight never carries Authorization.
        r = await client.request(
            "OPTIONS", "/api/tasks",
            headers={"Origin": "capacitor://localhost",
                     "Access-Control-Request-Method": "POST"})
        assert r.status_code == 200
    finally:
        monkeypatch.delenv("TASKMASTER_TOKEN")
        reload_settings()


async def test_token_still_does_not_let_a_stranger_in(monkeypatch):
    monkeypatch.setenv("TASKMASTER_TOKEN", "s3cret")
    reload_settings()
    try:
        async with client_from(OFF_NET) as c:
            r = await c.get("/api/tasks",
                            headers={"Authorization": "Bearer s3cret"})
            assert r.status_code == 403        # address is checked first
    finally:
        monkeypatch.delenv("TASKMASTER_TOKEN")
        reload_settings()


async def test_writes_are_gated_too(client, monkeypatch):
    await make(client, "before the gate")
    monkeypatch.setenv("TASKMASTER_TOKEN", "s3cret")
    reload_settings()
    try:
        # The round-5 endpoints are behind the same one gate as everything
        # else: only /health and the two app-update paths are exempt, and that
        # exemption is a property of the plugin, not of the path (middleware.py).
        for method, path in (("post", "/api/tasks"),
                             ("delete", "/api/tasks/x"),
                             ("get", "/api/meta"),
                             ("get", "/api/prefs"),
                             ("put", "/api/prefs"),
                             ("get", "/api/widget"),
                             ("post", "/api/categories/rename"),
                             ("post", "/api/categories/delete")):
            r = await getattr(client, method)(path)
            assert r.status_code == 401, path
    finally:
        monkeypatch.delenv("TASKMASTER_TOKEN")
        reload_settings()
