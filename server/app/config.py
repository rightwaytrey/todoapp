"""Environment-driven settings.

Deliberately not pydantic-settings: there are eight variables, half of them are
also read by deploy/install.sh and the systemd unit, and a plain os.environ read
keeps one obvious source of truth. Everything has a working default, so
`uvicorn app.main:app` with an empty environment runs against the user's real
Taskwarrior — which is what production wants (docs/design.md D1).

One exception: `bundle_public_base` has no default, because its value is the
user's own host name and this repo is public. See the comment on it.

TASKRC / TASKDATA are *not* read here on purpose. They are passed through to the
`task` child process by the OS, so the tests can point the whole server at a
throwaway data directory just by exporting them before importing the app.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger("taskmaster.config")

BASE_DIR = Path(__file__).resolve().parent.parent            # server/

# Loopback plus Tailscale's CGNAT range (docs/design.md D4). The phone reaches
# the box over WireGuard, so its source address is always a 100.64/10 one; a
# packet from anything else on the LAN has no business here.
DEFAULT_CIDRS = ["127.0.0.0/8", "::1/128", "100.64.0.0/10"]

# The five `pa` projects, always offered by /api/meta in this order even when
# nothing currently uses them (docs/api.md Meta). Mirrors pa_lib.PROJECTS.
PA_PROJECTS = ["personal", "work", "claude", "fun", "inbox"]

# `pa retag` paints these onto pending tasks every few minutes so the *old*
# Taskchamp filter has something to exact-match (docs/design.md, pa_lib
# retag_today). They are maintenance state, not user data: we strip them on the
# way out and ignore them on the way in so the client never learns they exist
# and can never clobber them with a tag replacement.
RESERVED_TAGS = frozenset({"today", "overdue", "due"})


def _s(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v


def _list(name: str) -> List[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


# --- Users (docs/api.md round 9, docs/design.md D19) -----------------------
USER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")

# The shared store's directory name, so no user may be called it.
SHARED = "shared"


@dataclass(frozen=True)
class UserSpec:
    """One `name=addr[,addr…]` entry of TASKMASTER_USERS, parsed.

    Only the parse lives here; what a user *owns* (a store, a prefs file) is
    app/stores.py's business, so this module stays "read the environment".
    """
    name: str
    addrs: Tuple[ipaddress._BaseAddress, ...]
    default: bool


def parse_users(raw: str) -> List[UserSpec]:
    """`TASKMASTER_USERS` -> specs, first entry the default user. Raises ValueError.

    **Fails loudly, on purpose.** Every other variable in this module falls
    back to a working default; this one cannot. An entry dropped for a typo
    makes that phone an *unmapped* address, and an unmapped address is the
    default user (docs/api.md round 9) — so a lenient parse turns "her name
    was misspelt" into "her phone shows your whole list". A service that
    refuses to start says so in the journal instead.
    """
    specs: List[UserSpec] = []
    seen_addrs = {}
    for entry in (e.strip() for e in raw.split(";")):
        if not entry:
            continue
        name, sep, addrs = entry.partition("=")
        name = name.strip()
        if not sep:
            raise ValueError("TASKMASTER_USERS entry %r is not name=addr[,addr]" % entry)
        if not USER_NAME_RE.match(name):
            raise ValueError("TASKMASTER_USERS name %r must match %s"
                             % (name, USER_NAME_RE.pattern))
        if name == SHARED:
            raise ValueError("TASKMASTER_USERS: %r is reserved for the shared store"
                             % SHARED)
        if any(s.name == name for s in specs):
            raise ValueError("TASKMASTER_USERS names %r twice" % name)
        parsed = []
        for a in (a.strip() for a in addrs.split(",")):
            if not a:
                continue
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                raise ValueError("TASKMASTER_USERS: %r (for %s) is not an IP address"
                                 % (a, name)) from None
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if ip in seen_addrs:
                # Two owners for one address: whichever won would be a guess.
                raise ValueError("TASKMASTER_USERS: %s is mapped to both %s and %s"
                                 % (ip, seen_addrs[ip], name))
            seen_addrs[ip] = name
            parsed.append(ip)
        specs.append(UserSpec(name, tuple(parsed), default=not specs))
    return specs


def _task_bin() -> str:
    """Same probe order as pa_lib._task_bin, so both layers find the same 3.4.2."""
    override = _s("TASKMASTER_TASK_BIN")
    if override:
        return override
    for c in (Path.home() / ".local/bin/task", Path("/usr/bin/task")):
        if c.exists():
            return str(c)
    return "task"


class Settings:
    """One mutable instance; tests poke attributes on it directly."""

    def __init__(self) -> None:
        self.app_name = "TaskMaster"
        self.version = "0.1.0"
        self.api_prefix = "/api"

        self.task_bin = _task_bin()
        self.task_timeout_s = float(_s("TASKMASTER_TASK_TIMEOUT", "20"))

        # Unset => no auth at all (docs/api.md Access). The tailnet allowlist is
        # the real gate; this is defence in depth for when the box is shared.
        self.token: Optional[str] = _s("TASKMASTER_TOKEN") or None

        self.allow_cidrs = list(DEFAULT_CIDRS) + _list("TASKMASTER_ALLOW_CIDRS")
        self.networks = []
        for c in self.allow_cidrs:
            try:
                self.networks.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                log.warning("ignoring unparseable CIDR %r in the allowlist", c)

        tzname = _s("TASKMASTER_TZ") or _s("TZ") or "America/Chicago"
        try:
            self.tz = ZoneInfo(tzname)
            self.tz_name = tzname
        except Exception:                                        # noqa: BLE001
            log.warning("unknown timezone %r, falling back to America/Chicago", tzname)
            self.tz = ZoneInfo("America/Chicago")
            self.tz_name = "America/Chicago"

        # docs/api.md List: completed = last 30 days, newest first, capped.
        self.completed_days = int(_s("TASKMASTER_COMPLETED_DAYS", "30"))
        self.completed_cap = int(_s("TASKMASTER_COMPLETED_CAP", "200"))

        self.cors_origins = _list("TASKMASTER_CORS_ORIGINS") or ["*"]

        # --- Preferences (docs/api.md round 5, docs/design.md D15) -----------
        # One JSON document, next to the env file the systemd unit already
        # reads. Not in Taskwarrior: none of this is task data, and a UDA per
        # setting would put the widget's row caps in every `task export`.
        # Not in the repo either — it is per-install state, and the repo is
        # public (D4).
        self.prefs_path = Path(
            _s("TASKMASTER_PREFS") or "~/.config/taskmaster/prefs.json"
        ).expanduser()

        # --- Users and stores (docs/api.md round 9, docs/design.md D19) -----
        # Unset (or empty) => no users, no shared store: single-user, exactly
        # as before round 9. The first entry is the default user, who owns the
        # ambient TASKRC/TASKDATA (~/.task, hooks and all) and `prefs_path`.
        self.users: List[UserSpec] = parse_users(_s("TASKMASTER_USERS"))
        # Where every OTHER store lives: <name>/{taskrc,data,prefs.json} per
        # non-default user, shared/{taskrc,data,categories.json}. Outside the
        # repo (it is task data) and outside ~/.task (whose hooks are pa's).
        self.stores_dir = Path(
            _s("TASKMASTER_STORES") or "~/.local/share/taskmaster"
        ).expanduser()

        # --- App bundle updates (docs/design.md D11, docs/api.md) -----------
        # Where scripts/publish_bundle.py writes manifest.json and the zips.
        # Gitignored, and empty until the planner says otherwise — an empty
        # directory is a server that tells every phone "no bundle published",
        # which is exactly the right answer before the first store build
        # carrying the plugin exists.
        self.bundles_dir = Path(
            _s("TASKMASTER_BUNDLES_DIR") or str(BASE_DIR / "bundles")
        ).expanduser()

        # The base of the download URL handed to the phone. ABSOLUTE, because
        # the app runs at capacitor://localhost and a relative URL would resolve
        # against the app itself.
        #
        # A constant rather than something derived from the request's Host: the
        # plugin's updateUrl is compiled into the shell (capacitor.config.ts),
        # so the phone always arrives at this exact name whatever the Settings
        # screen's server URL says, and an IP-literal Host would produce a
        # download URL that ATS refuses. **This must name the same host as that
        # updateUrl** — transitnav shipped bundles for three days to a store no
        # phone could reach because the two disagreed and nothing checked.
        #
        # NO DEFAULT IN CODE, and that is the one deliberate exception to this
        # module's "everything has a working default" rule. The value is the
        # user's own host name; this repo is public, so it lives in the
        # environment — ~/.config/taskmaster/env, which the systemd unit reads
        # through EnvironmentFile (deploy/taskmaster-api.service) and
        # scripts/publish_bundle.py reads directly. TASKMASTER_PUBLIC_BASE is
        # the same value under the name the publish script uses, accepted here
        # so one line in that file configures both.
        #
        # Unset is not an error: /api/app/update answers "no bundle published"
        # and logs a warning (routers/app_update.py). A server with no public
        # base has no absolute URL to offer, and offering a relative one would
        # send the phone to capacitor://localhost.
        self.bundle_public_base = (
            _s("TASKMASTER_BUNDLE_BASE") or _s("TASKMASTER_PUBLIC_BASE")
        ).rstrip("/")

        # Only this app gets answers; the plugin sends its own appId on every
        # check. Empty disables the check.
        self.bundle_app_id = _s("TASKMASTER_BUNDLE_APP_ID",
                                "org.rightwaytrey.taskmaster")


settings = Settings()


def reload_settings() -> Settings:
    """Re-read the environment in place. Used by the tests to flip the token."""
    new = Settings()
    settings.__dict__.update(new.__dict__)
    return settings
