"""L4 — ``build_router``: walk the table, register the routes (ADR-003).

Version-agnostic by construction: the prefix and the operations table are
**arguments**, so no ``/v1`` literal appears anywhere in ``registry/``. That is
what lets a future v2 reuse this engine untouched (doc 09 §9.1b).
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter
from starlette.responses import StreamingResponse

from ..settings import ServiceSettings
from .endpoints import make_endpoint, make_stream_endpoint
from .spec import OperationSpec


def build_router(
    operations: Sequence[OperationSpec],
    *,
    prefix: str,
    settings: ServiceSettings,
) -> APIRouter:
    """Generate the data-plane router for one API version.

    ``trusted_only`` operations are **not registered at all** when raw SQL is
    disabled — so they are absent from the route table *and* from the OpenAPI
    document, rather than present-but-forbidden. A capability nobody can see is
    a smaller surface than one that returns 403.
    """
    router = APIRouter(prefix=f"{prefix}/datasources")

    for spec in operations:
        if spec.trusted_only and not settings.api.enable_raw_sql:
            continue

        router.add_api_route(
            f"/{{name}}/{spec.suffix}",
            make_endpoint(spec),
            methods=["POST"],
            response_model=spec.response_model,
            summary=spec.summary,
            tags=[spec.tag],
            operation_id=f"{spec.op}_datasource",
        )

        if spec.streamable:
            router.add_api_route(
                f"/{{name}}/{spec.suffix}/stream",
                make_stream_endpoint(spec),
                methods=["POST"],
                summary=f"{spec.summary} (streamed)",
                tags=[spec.tag],
                operation_id=f"{spec.op}_datasource_stream",
                # The response is NDJSON/SSE bytes, not a model — declaring one
                # would make FastAPI try to validate a StreamingResponse.
                response_class=StreamingResponse,
            )

    return router


__all__ = ["build_router"]
