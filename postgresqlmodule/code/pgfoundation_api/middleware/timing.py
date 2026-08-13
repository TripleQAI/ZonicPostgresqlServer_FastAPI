"""L4 — request deadline + timing (P4.4, doc 06 §6.8).

A deadline that only exists client-side is not a timeout: the client gives up,
the server keeps working, and the connection stays busy. So the budget is
enforced here, and — critically — is also pushed **down to PostgreSQL** by the
data-access path via ``statement_timeout``.

Cancelling the coroutine is not the same as cancelling the query. Without the
database-level half, an abandoned request leaves PostgreSQL executing for a
socket nobody is reading.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class TimingMiddleware(BaseHTTPMiddleware):
    """Bound every request, and record how long it took."""

    def __init__(self, app, *, request_timeout_ms: int) -> None:
        super().__init__(app)
        self._timeout_s = request_timeout_ms / 1000.0

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        # OBSERVE (METRIC): pgf_http_requests_inflight{route} +1 / -1
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await call_next(request)
        except TimeoutError:
            # OBSERVE (METRIC): pgf_http_requests_total{status="503"}
            # OBSERVE (LOG):    request.timeout {route, budget_ms}
            return JSONResponse(
                {
                    "error": (f"request exceeded its {self._timeout_s * 1000:.0f}ms budget"),
                    "type": "TimeoutError",
                    "request_id": getattr(request.state, "request_id", None),
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # OBSERVE (METRIC): pgf_http_request_duration_seconds{route, method}
        # OBSERVE (METRIC): pgf_http_requests_total{route, method, status, version}
        response.headers["X-Elapsed-Ms"] = f"{elapsed_ms:.2f}"
        return response


__all__ = ["TimingMiddleware"]
