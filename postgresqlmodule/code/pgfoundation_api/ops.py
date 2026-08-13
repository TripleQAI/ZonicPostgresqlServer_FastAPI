"""L4 — UNVERSIONED operational endpoints (doc 09 §9.1b).

``/health``, ``/ready`` and ``/metrics`` are probe and scrape targets. They are
deliberately **not** under ``/v1``: a liveness probe pinned to ``/v1/health`` has
to be edited the day v1 is retired, which is precisely the day nobody wants to be
touching probes. ``/v1/health`` and ``/v1/ready`` exist as documented aliases for
parity with the Django surface, and are registered by the v1 router.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from pgfoundation import DataFoundation, HealthStatus

from .bootstrap.deps import get_foundation

router = APIRouter(tags=["ops"])

Overall = Literal["healthy", "degraded", "down"]


class HealthOut(BaseModel):
    status: Overall
    datasources: dict[str, str]


def roll_up(per_source: dict[str, HealthStatus]) -> Overall:
    """Collapse per-source status into one verdict (doc 06 §6.4).

    The Django shell reported ``all(healthy) ? "healthy" : "degraded"``, which can
    never return ``down`` even though the error model, the README and the
    ``HealthStatus`` enum all define it. Three states, honestly reported:
    """
    if not per_source:
        return "down"
    healthy = sum(1 for s in per_source.values() if s is HealthStatus.HEALTHY)
    if healthy == len(per_source):
        return "healthy"
    if healthy == 0:
        return "down"
    return "degraded"


async def _health(foundation: DataFoundation) -> HealthOut:
    # OBSERVE (METRIC): pgf_datasource_health{datasource} gauge (1 / 0.5 / 0)
    # OBSERVE (METRIC): pgf_health_check_duration_seconds{datasource}
    # OBSERVE (LOG):    datasource.health_changed {from, to} — ON CHANGE ONLY,
    #                   never once per poll (doc 11 §11.9)
    per_source = await foundation.health()
    return HealthOut(
        status=roll_up(per_source),
        datasources={name: status.value for name, status in per_source.items()},
    )


@router.get("/health", response_model=HealthOut, summary="Liveness")
async def health(
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
) -> HealthOut:
    """Liveness: ``200`` while the process is up, whatever the databases say.

    A liveness probe that fails when a *database* is unreachable causes the
    orchestrator to kill an otherwise healthy process — which fixes nothing and
    removes the thing that would have recovered. Database state belongs to
    readiness.
    """
    return await _health(foundation)


@router.get("/ready", response_model=HealthOut, summary="Readiness")
async def ready(
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
    response: Response,
) -> HealthOut:
    """Readiness: ``503`` when no data source is reachable."""
    out = await _health(foundation)
    if out.status == "down":
        response.status_code = 503
    return out


__all__ = ["router", "roll_up", "HealthOut"]
