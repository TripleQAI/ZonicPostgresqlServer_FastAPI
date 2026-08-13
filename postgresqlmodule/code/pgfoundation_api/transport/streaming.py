"""L4 — NDJSON and SSE framing over the core's async generators (ADR-006).

One internal stream, several wire formats, selected by ``Accept``. The core
already yields rows lazily from a server-side cursor; this module only frames
them.

**Backpressure is end-to-end and is the point.** The generator yields, the ASGI
server awaits the send, so a slow client throttles the database fetch. Memory
stays flat regardless of result size — no server-side buffer grows to compensate.

**Errors after the first byte cannot change the status.** Headers are already on
the wire by the time row 2 fails, so a failure is reported *in band* as a final
record. A silently truncated stream is indistinguishable from a complete one,
which is the failure mode this avoids (ADR-006 §4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse

from .serialization import dumps, dumps_line

NDJSON = "application/x-ndjson"
SSE = "text/event-stream"


def negotiate(request: Request) -> str:
    """Content negotiation as a Strategy: NDJSON unless SSE is asked for."""
    accept = request.headers.get("accept", "")
    return SSE if SSE in accept else NDJSON


async def _ndjson(rows: AsyncIterator[dict[str, Any]], request: Request) -> AsyncIterator[bytes]:
    sent = 0
    try:
        async for row in rows:
            # A disconnected client must stop the query, not just the transfer.
            # Without this the generator keeps pulling rows into a socket nobody
            # is reading, holding its connection for the full scan (ADR-006 §3).
            if await request.is_disconnected():
                break
            yield dumps_line(row)
            sent += 1
    except Exception as exc:  # noqa: BLE001 — reported in band, see module docstring
        # OBSERVE (LOG):    stream.failed {op, rows_sent, reason}
        # OBSERVE (METRIC): pgf_stream_terminations_total{reason="error"}
        yield dumps_line(
            {"error": {"type": type(exc).__name__, "message": str(exc)}, "partial": True}
        )
    # OBSERVE (METRIC): pgf_stream_rows_total{datasource, op} += sent
    # OBSERVE (METRIC): pgf_stream_duration_seconds, pgf_stream_connection_hold_seconds


async def _sse(rows: AsyncIterator[dict[str, Any]], request: Request) -> AsyncIterator[bytes]:
    try:
        async for row in rows:
            if await request.is_disconnected():
                break
            yield b"data: " + dumps(row) + b"\n\n"
    except Exception as exc:  # noqa: BLE001
        payload = dumps({"type": type(exc).__name__, "message": str(exc)})
        yield b"event: error\ndata: " + payload + b"\n\n"


def stream_response(
    rows: AsyncIterator[dict[str, Any]],
    request: Request,
    *,
    media_type: str | None = None,
) -> StreamingResponse:
    """Frame an async row iterator for the wire."""
    chosen = media_type or negotiate(request)
    body = _sse(rows, request) if chosen == SSE else _ndjson(rows, request)
    headers = {
        # Streaming responses must not be buffered by an intermediary, or
        # backpressure and liveness both disappear.
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(body, media_type=chosen, headers=headers)


__all__ = ["NDJSON", "SSE", "negotiate", "stream_response"]
