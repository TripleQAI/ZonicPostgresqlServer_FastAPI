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

from fastapi import Depends, Path

from pgfoundation import DataFoundation

from ..bootstrap.deps import get_foundation
from ..security.gate import Principal, authorize
from .spec import OperationSpec


def make_endpoint(spec: OperationSpec) -> Callable[..., Any]:
    """Build the unary endpoint for one operation."""

    async def endpoint(
        name: str,
        body: Any,
        foundation: DataFoundation,
        principal: Principal,  # noqa: ARG001 — presence enforces the auth check
    ) -> Any:
        # OBSERVE (TRACE):  span "pgf.operation" {op: spec.op, datasource: name}
        # OBSERVE (METRIC): pgf_http_requests_inflight{route} +1 / -1 around this body
        handle = foundation.datasource(name)
        result = await getattr(handle, spec.handle_method)(spec.to_core(body))
        # OBSERVE (METRIC): pgf_query_rows{datasource, op}
        # OBSERVE (LOG):    query.slow when elapsed_ms > observability.slow_query_ms
        # OBSERVE (METRIC): pgf_response_truncated_total{route} when rows exceed
        #                   api.max_response_rows (413 path — P3.8)
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


__all__ = ["make_endpoint"]
