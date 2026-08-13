"""L4 — request correlation (doc 11 §11.7).

One id threaded end to end, so a slow dashboard panel can be traced to a specific
statement. Echoed if the client supplies it, generated otherwise, and always
present on the response.

The last hop — into PostgreSQL ``application_name``, so a row in
``pg_stat_activity`` ties back to the HTTP request that caused it — is wired in
P5 with the rest of the observability work.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

HEADER = "X-Request-Id"


class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(HEADER) or uuid.uuid4().hex
        # Exception handlers read this off request.state to stamp error bodies.
        request.state.request_id = request_id
        # OBSERVE (LOG): every event in this request carries request_id
        # OBSERVE (TRACE): root span "http.request" {route, method, request_id},
        #                  continuing any inbound trace context
        response = await call_next(request)
        response.headers[HEADER] = request_id
        return response


__all__ = ["HEADER", "CorrelationMiddleware"]
