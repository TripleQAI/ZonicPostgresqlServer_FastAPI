"""v1 — hand-written admin routes.

These could not be generated from the operations table: they are not
per-datasource invocations. ``/v1/health`` and ``/v1/ready`` are documented
aliases of the unversioned probes, kept for parity with the Django surface
(doc 08 §8.1).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from pgfoundation import DataFoundation

from ....bootstrap.deps import get_foundation
from ....ops import HealthOut, _health
from ....security.gate import Principal, authorize_global
from ..schemas.responses import DataSourceList

router = APIRouter(tags=["admin"])


@router.get("/datasources", response_model=DataSourceList, summary="List data sources")
async def list_datasources(
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
    principal: Annotated[Principal, Depends(authorize_global("list"))],  # noqa: ARG001
) -> DataSourceList:
    return DataSourceList(datasources=foundation.names())


@router.get("/health", response_model=HealthOut, summary="Liveness (v1 alias)")
async def health(
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
) -> HealthOut:
    # Deliberately not _health() — see the unversioned /health's docstring
    # (pgfoundation_api/ops.py) for why liveness must not depend on DB latency.
    return HealthOut(
        status="healthy",
        datasources=dict.fromkeys(foundation.names(), "unknown"),
    )


@router.get("/ready", response_model=HealthOut, summary="Readiness (v1 alias)")
async def ready(
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
    response: Response,
) -> HealthOut:
    out = await _health(foundation)
    if out.status == "down":
        response.status_code = 503
    return out


__all__ = ["router"]
