"""L4 — response serialization (ADR-007).

**Where this is used.** FastAPI serializes normal responses through Pydantic,
which is faster than a custom response class (``ORJSONResponse`` is deprecated)
and already renders ``Decimal`` as a JSON string. So this module is *not* the
app-wide response class.

It exists for the paths that bypass Pydantic and must write bytes themselves —
NDJSON and SSE streaming (P3), where a ``StreamingResponse`` yields encoded rows
one at a time. Those paths need exactly the same type rules, so the rules live
here once rather than being re-derived per stream.

**The contract that must not break.** PostgreSQL ``numeric`` arrives as a
:class:`decimal.Decimal` and is serialized as a JSON **string** (``"42.00"``) to
preserve exact precision. Clients parse it that way. Swapping the encoder is
precisely how ``"42.00"`` silently becomes ``42.0`` and money becomes lossy — the
change is invisible in every test that does not assert on the JSON type. So the
rule is explicit here, and ``test_numeric_serializes_as_string`` guards it.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from decimal import Decimal
from typing import Any

import orjson


def _iso_duration(delta: timedelta) -> str:
    """ISO-8601 duration. Explicit rather than accidental."""
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    days, hours = divmod(hours, 24)
    out = f"{sign}P{days}D" if days else f"{sign}P"
    out += "T"
    out += f"{hours}H" if hours else ""
    out += f"{minutes}M" if minutes else ""
    out += f"{seconds}S" if seconds or out.endswith("T") else ""
    return out


def default(obj: Any) -> Any:
    """Encoder for types orjson does not handle natively (ADR-007).

    ``datetime`` / ``date`` / ``time`` / ``UUID`` are native to orjson and are
    deliberately absent here — adding them would silently override RFC 3339.
    """
    if isinstance(obj, Decimal):
        # THE contract. Never float(obj).
        return str(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, timedelta):
        return _iso_duration(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    raise TypeError(f"cannot serialize {type(obj).__name__} — add a rule in ADR-007")


def dumps(content: Any) -> bytes:
    return orjson.dumps(content, default=default)


def dumps_line(content: Any) -> bytes:
    """One NDJSON line, newline included (P3)."""
    return dumps(content) + b"\n"


__all__ = ["default", "dumps", "dumps_line"]
