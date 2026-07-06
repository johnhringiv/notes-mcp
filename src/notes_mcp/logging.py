"""Structured JSON logging to stdout.

Every tool invocation logs one line containing ``tool``, ``note_id``,
``duration_ms``, ``status``, and ``request_id`` on every call. Docker
captures stdout; the NAS handles retention.
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
import time
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


logger = logging.getLogger("notes_mcp")


def _log_outcome(func_name: str, note_id: Any, start: float, request_id: str, result: Any) -> None:
    status = result.get("error", "ok") if isinstance(result, dict) else "ok"
    logger.info(
        "tool call",
        extra={
            "tool": func_name,
            "note_id": note_id,
            "duration_ms": round((time.monotonic() - start) * 1000, 1),
            "status": status,
            "request_id": request_id,
        },
    )


def _log_failure(func_name: str, note_id: Any, start: float, request_id: str) -> dict[str, Any]:
    logger.exception(
        "tool failed",
        extra={
            "tool": func_name,
            "note_id": note_id,
            "duration_ms": round((time.monotonic() - start) * 1000, 1),
            "status": "internal_error",
            "request_id": request_id,
        },
    )
    return {"error": "internal_error", "details": {}}


def _extract_note_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    note_id = kwargs.get("note_id")
    if note_id is None and args and isinstance(args[0], str):
        note_id = args[0]
    return note_id


def logged_tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a (sync or async) tool handler: time it, log one line, never raise.

    Unexpected exceptions become ``{"error": "internal_error", ...}`` so the
    MCP client always gets a structured response.
    """
    func_name = getattr(func, "__name__", "tool")

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            request_id = uuid.uuid4().hex[:12]
            note_id = _extract_note_id(args, kwargs)
            start = time.monotonic()
            try:
                result: dict[str, Any] = await func(*args, **kwargs)
            except Exception:
                return _log_failure(func_name, note_id, start, request_id)
            _log_outcome(func_name, note_id, start, request_id, result)
            return result

        return async_wrapper

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        request_id = uuid.uuid4().hex[:12]
        note_id = _extract_note_id(args, kwargs)
        start = time.monotonic()
        try:
            result: dict[str, Any] = func(*args, **kwargs)
        except Exception:
            return _log_failure(func_name, note_id, start, request_id)
        _log_outcome(func_name, note_id, start, request_id, result)
        return result

    return wrapper
