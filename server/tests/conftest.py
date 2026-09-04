"""Test harness: a throwaway Taskwarrior database, driven by the real binary.

Nothing here mocks `task`. The whole point of the server is the shape of what
Taskwarrior 3.4.2 actually prints, so a mock would only ever assert that this
file agrees with itself.

**The safety rail matters more than the fixtures.** The production database is
~/.task and it holds real work, so before a single command runs we assert that
TASKDATA is a fresh directory under the system temp dir and is not the real
one. `hooks=off` in the throwaway taskrc keeps the on-add/on-modify hooks from
firing pa-pushnow (which would `task sync` and republish the phone widget) on
every test task.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

REAL_TASKDATA = (Path.home() / ".task").resolve()

_TMP = Path(tempfile.mkdtemp(prefix="taskmaster-tests-")).resolve()
DATA = _TMP / "data"
DATA.mkdir()
RC = _TMP / "taskrc"
RC.write_text(
    "data.location=%s\n"
    "hooks=off\n"
    "confirmation=off\n"
    "recurrence=on\n"
    # The manual-order UDA (docs/api.md "Ordering moves to the server"), the
    # same two lines deploy/install.sh puts in the real ~/.taskrc. They are not
    # optional here: with the UDA undeclared, `task <uuid> modify order:1500`
    # exits 0 and rewrites the task's DESCRIPTION to "order:1500", so the tests
    # would pass against a Taskwarrior doing something quite different from
    # production.
    "uda.order.type=numeric\n"
    "uda.order.label=Order\n" % DATA
)

# --- the rail. Never let a test touch the real ~/.task. ---------------------
assert DATA.is_relative_to(Path(tempfile.gettempdir()).resolve()), DATA
assert DATA != REAL_TASKDATA, DATA
assert REAL_TASKDATA not in DATA.parents, DATA

PREFS = _TMP / "prefs.json"

os.environ["TASKRC"] = str(RC)
os.environ["TASKDATA"] = str(DATA)
os.environ["TASKMASTER_TZ"] = "America/Chicago"
# The same rail as TASKDATA, for the same reason: the real file is
# ~/.config/taskmaster/prefs.json and a test that wrote it would rearrange the
# user's own category order.
os.environ["TASKMASTER_PREFS"] = str(PREFS)
os.environ.pop("TASKMASTER_TOKEN", None)
os.environ.pop("TASKMASTER_ALLOW_CIDRS", None)

atexit.register(shutil.rmtree, _TMP, True)

import pytest                                                    # noqa: E402
import pytest_asyncio                                            # noqa: E402
from httpx import ASGITransport, AsyncClient                     # noqa: E402

from app.config import reload_settings, settings                 # noqa: E402

# Second rail, after the app has resolved its own settings: prove the binary
# the server will run is pointed at the throwaway store.
assert os.environ["TASKDATA"] == str(DATA)

assert settings.prefs_path == PREFS, settings.prefs_path

TASK = settings.task_bin


def task(*args: str) -> subprocess.CompletedProcess:
    """Drive the CLI directly, for arranging a fixture or checking a result."""
    return subprocess.run(
        [TASK, "rc.confirmation=off", "rc.recurrence.confirmation=off",
         "rc.context=none", "rc.verbose=nothing", *args],
        capture_output=True, text=True, check=False)


@pytest.fixture(autouse=True)
def clean_db():
    """One empty database, and one un-configured server, per test."""
    for child in DATA.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink()
    # No prefs file: every test starts from the documented defaults, the same
    # state a freshly installed box is in.
    PREFS.unlink(missing_ok=True)
    reload_settings()
    yield


@pytest_asyncio.fixture
async def client():
    """Loopback by default — httpx's ASGITransport reports 127.0.0.1."""
    from app.main import app

    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with AsyncClient(transport=transport,
                           base_url="http://testserver") as c:
        yield c


def client_from(host: str, port: int = 1234) -> AsyncClient:
    """A client that presents an arbitrary peer address, for the allowlist."""
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app, client=(host, port)),
                       base_url="http://testserver")


# --- helpers ---------------------------------------------------------------

async def make(c: AsyncClient, description: str, **fields) -> dict:
    body = {"description": description}
    body.update(fields)
    r = await c.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def listing(c: AsyncClient, status: str = "pending") -> list:
    r = await c.get("/api/tasks", params={"status": status})
    assert r.status_code == 200, r.text
    return r.json()
