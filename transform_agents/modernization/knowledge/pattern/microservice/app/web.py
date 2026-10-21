"""Request helpers for the domain API: validation (400), admin auth (403) and timestamps."""

import hmac
from datetime import UTC, datetime
from typing import Any

from flask import abort, current_app, request


def json_body(*required: str) -> dict[str, Any]:
    """The JSON object sent by the client; 400 if it is not an object or misses a required field."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "expected a JSON object")
    missing = [name for name in required if name not in data]
    if missing:
        abort(400, f"missing field(s): {', '.join(missing)}")
    return data


def int_field(data: dict[str, Any], name: str, *, minimum: int = 0, default: int | None = None) -> int:
    """An integer field >= minimum (bools and strings are rejected with 400)."""
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        abort(400, f"{name} must be an integer >= {minimum}")
    return value


def float_field(data: dict[str, Any], name: str, *, minimum: float = 0.0, default: float | None = None) -> float:
    """A numeric field >= minimum, returned as float."""
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < minimum:
        abort(400, f"{name} must be a number >= {minimum}")
    return float(value)


def require_admin_token(header: str = "X-Admin-Token") -> None:
    """403 unless the header matches the ADMIN_TOKEN secret (constant-time compare; no token configured = deny)."""
    expected = current_app.config["SETTINGS"].admin_token
    supplied = request.headers.get(header, "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        abort(403, "forbidden")


def utc_now_iso() -> str:
    """Naive UTC ISO-8601 timestamp, the same format as the legacy datetime.utcnow().isoformat()."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat()
