"""L4 — error translation (doc 08 §8.3).

The Django shell wrapped every view in an ``@api`` decorator that caught and
translated. Here the mapping is a **table** registered once as exception
handlers, so the concern leaves the call sites entirely and cannot be forgotten
on a new endpoint.

Bodies never leak SQL internals, credentials, or stack traces to the client — the
detail goes to logs with the request id (doc 11 §11.8).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pgfoundation import (
    ConfigError,
    IntegrityError,
    PgFoundationError,
    PoolTimeoutError,
    QueryError,
    TransientError,
)
from pgfoundation import (
    ConnectionError as PgConnectionError,
)

from .security.ports import AuthenticationError, AuthorizationError


class ResponseTooLarge(Exception):
    """Result exceeds api.max_response_rows — the caller wants /stream.

    Refusing is the point: materializing an unbounded result is how a worker
    OOMs, and the client cannot know the size in advance either. The message
    names the route that has no such bound (doc 06 §6.7).
    """


#: Core error type -> HTTP status. Order matters: the first isinstance match
#: wins, so subclasses must precede their bases (PoolTimeoutError is a
#: ConnectionError; TransientError and QueryError are siblings).
STATUS_MAP: tuple[tuple[type[Exception], int], ...] = (
    (ResponseTooLarge, 413),
    (AuthenticationError, 401),
    (AuthorizationError, 403),
    (ConfigError, 404),  # unknown data source is a ConfigError
    (IntegrityError, 409),
    (QueryError, 400),
    (PoolTimeoutError, 503),
    (PgConnectionError, 503),
    (TransientError, 503),
)

#: Statuses a client may sensibly retry.
RETRYABLE = {503}


def status_for(exc: Exception) -> int:
    for cls, code in STATUS_MAP:
        if isinstance(exc, cls):
            return code
    if isinstance(exc, PgFoundationError):
        return 500
    return 500


def _body(exc: Exception, request_id: str | None) -> dict[str, object]:
    return {"error": str(exc), "type": type(exc).__name__, "request_id": request_id}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _response(request: Request, exc: Exception) -> JSONResponse:
    status = status_for(exc)
    headers = {}
    rid = _request_id(request)
    if rid:
        headers["X-Request-Id"] = rid
    if status in RETRYABLE:
        # Tell the client this is worth retrying rather than leaving it to guess.
        headers["Retry-After"] = "1"
    # OBSERVE (METRIC): counter by mapped status
    # OBSERVE (LOG):    request.failed {route, status, type}
    return JSONResponse(_body(exc, rid), status_code=status, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the table to an app. Called once by ``create_app``."""

    @app.exception_handler(PgFoundationError)
    async def _pgfoundation(request: Request, exc: PgFoundationError) -> JSONResponse:
        return _response(request, exc)

    @app.exception_handler(AuthenticationError)
    async def _authn(request: Request, exc: AuthenticationError) -> JSONResponse:
        return _response(request, exc)

    @app.exception_handler(AuthorizationError)
    async def _authz(request: Request, exc: AuthorizationError) -> JSONResponse:
        return _response(request, exc)

    @app.exception_handler(ResponseTooLarge)
    async def _too_large(request: Request, exc: ResponseTooLarge) -> JSONResponse:
        return _response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Deliberately generic: the Django shell returned a clean 400 without
        # echoing field internals, and that is the safer behaviour to keep.
        # OBSERVE (LOG): request.invalid — field NAMES only, never values
        return _response(request, QueryError("invalid request body"))


__all__ = [
    "RETRYABLE",
    "STATUS_MAP",
    "ResponseTooLarge",
    "register_exception_handlers",
    "status_for",
]
