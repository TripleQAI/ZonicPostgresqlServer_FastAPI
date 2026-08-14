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
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class TimingMiddleware(BaseHTTPMiddleware):
    """Bound every request, and record how long it took."""

    def __init__(self, app, *, request_timeout_ms: int, metrics: Any = None) -> None:
        super().__init__(app)
        self._timeout_s = request_timeout_ms / 1000.0
        self._metrics = metrics

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        metrics = self._metrics or request.app.state.metrics
        # Deliberately UNLABELLED. The route template is not resolvable yet:
        # middleware runs before routing, so `scope["route"]` is absent here and
        # the only label available would be the concrete path — which embeds the
        # data-source name and mints a series per source per route (doc 11 §11.9).
        # Total in-flight is also the number that actually drives capacity
        # decisions; per-route in-flight rarely is.
        metrics.incr("pgf_http_requests_inflight", 1)
        try:
            async with asyncio.timeout(self._timeout_s):
                response = await call_next(request)
        except TimeoutError:
            # OBSERVE (METRIC): pgf_http_requests_total{status="503"}
            # OBSERVE (LOG):    request.timeout {route, budget_ms}
            metrics.incr(
                "pgf_http_requests_total",
                1,
                route=_route_template(request),
                method=request.method,
                status="503",
            )
            return JSONResponse(
                {
                    "error": (f"request exceeded its {self._timeout_s * 1000:.0f}ms budget"),
                    "type": "TimeoutError",
                    "request_id": getattr(request.state, "request_id", None),
                },
                status_code=503,
                headers={"Retry-After": "1"},
            )
        finally:
            metrics.incr("pgf_http_requests_inflight", -1)

        elapsed = time.perf_counter() - started
        # Read AFTER call_next: routing has run by now and mutated the same scope
        # dict in place, so the template is available.
        route = _route_template(request)
        metrics.observe(
            "pgf_http_request_duration_seconds", elapsed, route=route, method=request.method
        )
        metrics.incr(
            "pgf_http_requests_total",
            1,
            route=route,
            method=request.method,
            status=str(response.status_code),
            version=_api_version(request),
        )
        response.headers["X-Elapsed-Ms"] = f"{elapsed * 1000:.2f}"
        return response


__all__ = ["TimingMiddleware"]


def _route_template(request: Request) -> str:
    """The route pattern, not the concrete path (doc 11 §11.9).

    Only meaningful once routing has run — call this AFTER ``call_next``. The
    fallback exists for requests that matched no route (404s), where the pattern
    genuinely does not exist; those fold to the raw path, which is bounded in
    practice by the 404 being rare.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


def _api_version(request: Request) -> str:
    """Which mounted API version served this — the label that answers
    "is anyone still on v1?" before it is switched off (doc 09 §9.1b)."""
    segments = request.url.path.strip("/").split("/")
    return segments[0] if segments and segments[0].startswith("v") else "none"
