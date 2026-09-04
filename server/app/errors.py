"""One error shape everywhere: {"error": code, "detail": text} (docs/api.md)."""
from __future__ import annotations

from typing import List

from fastapi import HTTPException


def api_error(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "detail": detail})


def bad_request(detail: str, code: str = "bad_request") -> HTTPException:
    return api_error(400, code, detail)


def not_found(detail: str = "No task with that uuid.") -> HTTPException:
    return api_error(404, "not_found", detail)


def conflict(detail: str) -> HTTPException:
    return api_error(409, "conflict", detail)


def invalid(field: str, detail: str) -> HTTPException:
    """422 whose detail names the field, as docs/api.md promises."""
    return api_error(422, "invalid_request", f"{field}: {detail}")


class TaskFailed(Exception):
    """The `task` CLI exited non-zero (or could not be run at all).

    Surfaces as 502 task_failed. Taskwarrior 3.4.2 prints its complaints on
    *stdout*, not stderr ("Task <id> ... is neither pending nor waiting."), so
    the caller hands us whichever stream actually said something.
    """

    def __init__(self, detail: str, argv: List[str], returncode: int = -1) -> None:
        super().__init__(detail)
        self.detail = detail or "task exited %d with no output" % returncode
        self.argv = argv
        self.returncode = returncode
