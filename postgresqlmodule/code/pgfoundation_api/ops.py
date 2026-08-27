"""L4 — UNVERSIONED operational endpoints (doc 09 §9.1b).

``/health``, ``/ready`` and ``/metrics`` are probe and scrape targets. They are
deliberately **not** under ``/v1``: a liveness probe pinned to ``/v1/health`` has
to be edited the day v1 is retired, which is precisely the day nobody wants to be
touching probes. ``/v1/health`` and ``/v1/ready`` exist as documented aliases for
parity with the Django surface, and are registered by the v1 router.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
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

    Deliberately does NOT call ``_health()`` (unlike ``/ready``) — that performs a
    real ``SELECT 1`` per datasource, which can legitimately take seconds under
    WAN latency to a remote database (Tiger Cloud) or a still-warming pool right
    after startup. Calling it here would make liveness exactly as DB-latency-
    dependent as readiness, contradicting this docstring's own reasoning above —
    confirmed in real use, 2026-08-27: a cold pool on a fresh pod made /health
    itself slow enough to blow past the probe's timeout, triggering the exact
    kill-a-healthy-process cycle this endpoint exists to avoid. ``datasources``
    lists known names without probing them, so the response shape stays stable
    for any client already parsing it.
    """
    return HealthOut(
        status="healthy",
        datasources=dict.fromkeys(foundation.names(), "unknown"),
    )


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


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
async def metrics(request: Request) -> PlainTextResponse:
    """Scrape endpoint (P5.6).

    Unversioned, like the probes: a scrape config pinned to ``/v1/metrics`` would
    have to be edited the day v1 retires (doc 09 §9.1b).

    Empty body when ``observability.metrics_enabled`` is false — the Null Object
    has nothing to render, and that is the zero-cost default ADR-014 depends on
    rather than an error condition.
    """
    collector = request.app.state.metrics
    body = collector.render() if hasattr(collector, "render") else ""
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


__all__ = ["router", "roll_up", "HealthOut"]
