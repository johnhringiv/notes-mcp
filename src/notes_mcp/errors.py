"""Structured error objects returned from tool handlers.

Convention: tools never raise out of their handlers. They
return ``{"error": <category>, "details": <context>}`` instead.
"""

from __future__ import annotations

from typing import Any


def error(category: str, **details: Any) -> dict[str, Any]:
    """Build a structured error dict with a category and free-form details."""
    return {"error": category, "details": details}
