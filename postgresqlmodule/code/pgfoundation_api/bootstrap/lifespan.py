"""L4 — application lifespan: build the object graph, drain it on exit (ADR-005).

The foundation is built **on the serving event loop**, never at import time, so
pools bind to the loop that will actually use them. This replaces the Django
shell's hand-rolled ASGI ``lifespan`` message loop and its module-global
singletons; everything lands on ``app.state`` and is reached through ``Depends``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pgfoundation import ConfigError, DataFoundation

from ..settings import ServiceSettings

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: build + open the foundation. Shutdown: drain it."""
    settings: ServiceSettings = app.state.settings
    started = time.perf_counter()

    # OBSERVE (LOG):    service.startup {version, profile, datasources, workers}
    # OBSERVE (METRIC): pgf_startup_duration_seconds
    # OBSERVE: bind the external project's Metrics/Logger/Tracer adapters HERE —
    #          the composition root is the one place that knows the concrete
    #          observability provider (doc 11 §11.11 / ADR-014).

    # A foundation may be injected before startup (tests supply a fake-driver one).
    if getattr(app.state, "foundation", None) is None:
        if not settings.config_path:
            raise ConfigError(
                "no config: set PGF_CONFIG (or pass config_path) so the foundation "
                "knows which data sources to open"
            )
        app.state.foundation = await DataFoundation.from_config(settings.config_path)
        app.state.owns_foundation = True
    else:
        # Injected — whoever injected it is responsible for closing it.
        app.state.owns_foundation = False

    app.state.startup_seconds = time.perf_counter() - started

    try:
        yield
    finally:
        # OBSERVE (LOG):    service.shutdown {inflight, drained_ms, forced}
        # OBSERVE (METRIC): pgf_shutdown_drain_seconds
        # P4.5 replaces this with a bounded drain that waits for in-flight work.
        if app.state.owns_foundation and app.state.foundation is not None:
            await app.state.foundation.aclose()


__all__ = ["lifespan"]
