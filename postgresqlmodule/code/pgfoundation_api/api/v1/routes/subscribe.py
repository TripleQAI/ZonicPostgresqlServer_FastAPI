"""v1 — ``GET /v1/datasources/{name}/subscribe/{channel}`` (ADR-006).

A ``LISTEN/NOTIFY`` change feed over Server-Sent Events. Hand-written because it
is a GET with no request body and an unbounded lifetime — it is not an operation
in the registry sense.

SSE is the right choice for browsers: native ``EventSource``, automatic
reconnect, plain HTTP, no protocol upgrade. The feed is one-way, so WebSockets
would add a bidirectional channel nobody uses.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from starlette.responses import StreamingResponse

from pgfoundation import DataFoundation

from ....bootstrap.deps import get_foundation
from ....security.gate import Principal, authorize
from ....transport.streaming import SSE, stream_response

router = APIRouter(tags=["stream"])


@router.get(
    "/datasources/{name}/subscribe/{channel}",
    summary="Subscribe to a LISTEN/NOTIFY channel (SSE)",
    response_class=StreamingResponse,
)
async def subscribe(
    request: Request,
    name: str,
    channel: Annotated[str, Path(description="Channel name; validated as an identifier")],
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
    principal: Annotated[Principal, Depends(authorize("read"))],  # noqa: ARG001
) -> StreamingResponse:
    handle = foundation.datasource(name)

    async def events():
        # OBSERVE (METRIC): pgf_notify_events_total{datasource, channel}
        # OBSERVE (METRIC): pgf_stream_connection_hold_seconds{datasource} — a
        #                   subscription pins a connection for its whole lifetime
        # OBSERVE (LOG):    listen.resubscribed on reconnect
        async for event in handle.listen(channel):
            yield {"channel": event.channel, "payload": event.payload, "pid": event.pid}

    # Always SSE here: the client is an EventSource, not a bulk reader.
    return stream_response(events(), request, media_type=SSE)


__all__ = ["router"]
