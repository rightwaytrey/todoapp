"""GET/PUT /api/prefs — the one settings document (docs/design.md D15).

Two lines of code and one file, and it replaces a synchronisation problem: the
phone, the widget and the server all read the same document, so a preference
changed on the Settings screen is in effect on the home screen at the widget's
next refresh with nothing to push and nothing to reconcile.

`PUT` replaces the whole document rather than patching it — the client holds
the entire object anyway (it is under a kilobyte), and a partial update would
need a merge rule per section for no gain.
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import prefs as store
from ..prefs import Prefs

router = APIRouter(tags=["prefs"])


@router.get("/prefs")
async def get_prefs():
    """The stored document with every default filled in.

    Never 404 and never 500: a box with no file yet answers with the defaults,
    which is what makes "first launch" and "already configured" the same code
    path on the client.
    """
    return store.as_json(store.load())


@router.put("/prefs")
async def put_prefs(body: Prefs):
    """Validate, fill defaults, write atomically, answer with what was stored.

    Answering with the stored document rather than `204` is deliberate: the
    client sent a partial-looking object and gets back exactly what the widget
    will read, so a dropped unknown key or a defaulted section is visible at
    once instead of at the next launch.
    """
    return store.as_json(store.save(body))
