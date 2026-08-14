"""L4 — ``Depends`` providers (ADR-005).

Every dependency an endpoint needs is declared in its signature and resolved from
``app.state``. There are no module-level globals and no setter functions, so two
differently-configured apps can coexist in one process and each test gets an
isolated instance.
"""

from __future__ import annotations

from fastapi import Request

from pgfoundation import DataFoundation

from ..settings import ServiceSettings


def get_settings(request: Request) -> ServiceSettings:
    return request.app.state.settings


def get_foundation(request: Request) -> DataFoundation:
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is None:  # pragma: no cover — lifespan guarantees this
        raise RuntimeError("DataFoundation is not initialised; lifespan did not run")
    return foundation


def get_metrics(request: Request):
    """The bound MetricsPort. A Null Object unless metrics are enabled."""
    return request.app.state.metrics


__all__ = ["get_foundation", "get_metrics", "get_settings"]
