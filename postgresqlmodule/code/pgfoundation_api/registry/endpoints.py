"""L4 — the generic endpoint factory (ADR-003, P1.9).

The five steps every data-plane endpoint performs — authorize, validate, map DTO
to core, call the Facade, serialize — exist **here, once**. The nine
near-identical Django views collapse into this plus one data row each.

## Why the signature is rebuilt by hand

FastAPI derives everything — validation, dependency injection, the OpenAPI request
schema — from the endpoint's *signature*. A closure like::

    async def endpoint(body: spec.request_model): ...

does not work. This module uses ``from __future__ import annotations`` (as all our
modules do), so annotations are stored as **strings**; FastAPI would try to resolve
the literal name ``"spec.request_model"`` in the module namespace, fail to find a
type, and silently treat ``body`` as a query parameter — producing an endpoint that
400s on every call and an OpenAPI document with no request schema at all.

The failure is silent, which is why ``test_openapi_documents_every_operation``
exists: it is the detector for exactly this.

So the factory builds an explicit :class:`inspect.Signature` with the concrete
model object as the annotation, and assigns both ``__signature__`` and
``__annotations__``. FastAPI reads ``__signature__``; the real annotations are set
too so other introspection tools agree with it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Path, Request

from pgfoundation import DataFoundation

from ..bootstrap.deps import get_foundation, get_settings
from ..errors import ResponseTooLarge
from ..security.gate import Principal, authorize
from ..settings import ServiceSettings
from ..transport.streaming import stream_response
from .spec import OperationSpec


def make_endpoint(spec: OperationSpec) -> Callable[..., Any]:
    """Build the unary endpoint for one operation."""

    async def endpoint(
        name: str,
        body: Any,
        foundation: DataFoundation,
        settings: Any,
        principal: Principal,  # noqa: ARG001 — presence enforces the auth check
    ) -> Any:
        # OBSERVE (TRACE):  span "pgf.operation" {op: spec.op, datasource: name}
        # OBSERVE (METRIC): pgf_http_requests_inflight{route} +1 / -1 around this body
        handle = foundation.datasource(name)
        result = await spec.call(handle, body)
        # OBSERVE (METRIC): pgf_query_rows{datasource, op}
        # OBSERVE (LOG):    query.slow when elapsed_ms > observability.slow_query_ms
        rows = getattr(result, "rows", None)
        limit = settings.api.max_response_rows
        if rows is not None and len(rows) > limit:
            # OBSERVE (METRIC): pgf_response_truncated_total{route}
            # OBSERVE (LOG):    response.too_large {rows, limit}
            message = f"result has {len(rows)} rows; the configured maximum for a single response is {limit}"
            if spec.streamable:
                # Point at the route that has no such bound, rather than leaving
                # the caller to discover it.
                message += f". Use POST .../{{name}}/{spec.suffix}/stream instead"
            raise ResponseTooLarge(message)
        return spec.to_response(result)

    # --- the part that makes FastAPI see a real, typed endpoint --------------
    parameters = [
        inspect.Parameter(
            "name",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[str, Path(description="Configured data-source name")],
        ),
        inspect.Parameter(
            "body",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=spec.request_model,
        ),
        inspect.Parameter(
            "foundation",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[DataFoundation, Depends(get_foundation)],
        ),
        inspect.Parameter(
            "settings",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[ServiceSettings, Depends(get_settings)],
        ),
        inspect.Parameter(
            "principal",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            # The action comes from the descriptor, so an endpoint physically
            # cannot be registered without its authorization check.
            annotation=Annotated[Principal, Depends(authorize(spec.action))],
        ),
    ]
    endpoint.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=spec.response_model
    )
    endpoint.__annotations__ = {p.name: p.annotation for p in parameters}
    endpoint.__annotations__["return"] = spec.response_model
    endpoint.__name__ = f"{spec.op}_endpoint"
    endpoint.__doc__ = spec.summary or f"Invoke `{spec.op}` on a named data source."
    return endpoint


__all__ = ["make_endpoint", "make_stream_endpoint"]


def make_stream_endpoint(spec: OperationSpec) -> Callable[..., Any]:
    """Build the streaming variant of one operation (ADR-006, P3.3).

    Same descriptor, same auth action, same DTO — only the framing differs. The
    core already yields rows lazily from a server-side cursor; this returns a
    ``StreamingResponse`` over that generator so memory stays flat and
    backpressure reaches the database.
    """

    async def endpoint(
        request: Request,
        name: str,
        body: Any,
        foundation: DataFoundation,
        principal: Principal,  # noqa: ARG001 — presence enforces the auth check
    ) -> Any:
        # OBSERVE (METRIC): pgf_stream_active{datasource} gauge +1 / -1
        # OBSERVE (TRACE):  span "pgf.stream" {op, datasource}
        handle = foundation.datasource(name)
        # compile_spec keeps the shell out of `_internal` while still letting it
        # stream a governed object rather than only raw SQL.
        query = handle.compile_spec(spec.to_core(body))
        rows = handle.stream(query)
        return stream_response(rows, request)

    parameters = [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        inspect.Parameter(
            "name",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[str, Path(description="Configured data-source name")],
        ),
        inspect.Parameter(
            "body", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=spec.request_model
        ),
        inspect.Parameter(
            "foundation",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[DataFoundation, Depends(get_foundation)],
        ),
        inspect.Parameter(
            "principal",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[Principal, Depends(authorize(spec.action))],
        ),
    ]
    endpoint.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    endpoint.__annotations__ = {p.name: p.annotation for p in parameters}
    endpoint.__name__ = f"{spec.op}_stream_endpoint"
    endpoint.__doc__ = (
        f"Stream `{spec.op}` at constant memory. Responds with NDJSON "
        f"(`application/x-ndjson`), or SSE when `Accept: text/event-stream`. "
        f"A mid-stream failure is reported in band as a final "
        f'`{{"error": ..., "partial": true}}` record, because the status line has '
        f"already been sent."
    )
    return endpoint
