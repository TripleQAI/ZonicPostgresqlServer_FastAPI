"""L4 — the application factory (ADR-005).

``create_app(settings)`` builds a fully-wired FastAPI application. Nothing is
constructed at import time, so:

* each uvicorn worker builds its own app and its own pools, on its own event loop;
* tests build an isolated app per test with no global state to reset.

Run it with ``uvicorn pgfoundation_api.app:create_app --factory``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from . import ops
from .api import versions
from .bootstrap.lifespan import lifespan
from .errors import register_exception_handlers
from .middleware.correlation import CorrelationMiddleware
from .security.gate import build_auth_gate
from .settings import ServiceSettings, load_service_settings

if TYPE_CHECKING:  # pragma: no cover
    from pgfoundation import DataFoundation


def create_app(
    settings: ServiceSettings | None = None,
    *,
    foundation: DataFoundation | None = None,
    **kwargs: Any,
) -> FastAPI:
    """Build the application.

    ``foundation`` lets a caller inject an already-built ``DataFoundation`` —
    tests pass one backed by ``FakeDriver`` so the whole shell runs with no
    database. When injected, the injector owns its lifecycle.
    """
    settings = settings or load_service_settings()

    app = FastAPI(
        title=settings.api.title,
        version=settings.api.version,
        root_path=settings.api.root_path,
        lifespan=lifespan,
        # Per-version OpenAPI documents are mounted in P1.10; the app-level docs
        # are config-gated here (prod turns them off).
        docs_url="/docs" if settings.api.docs_enabled else None,
        redoc_url="/redoc" if settings.api.docs_enabled else None,
        # No custom response class: FastAPI serializes through Pydantic, which
        # is faster than routing everything via ORJSONResponse (now deprecated)
        # AND preserves the `Decimal -> "42.00"` contract. Verified by
        # test_numeric_serializes_as_string, not assumed. See ADR-007.
        **kwargs,
    )

    # The composition root: everything the request path needs hangs off state and
    # is reached via Depends — never via a module-level global.
    app.state.settings = settings
    app.state.foundation = foundation
    app.state.auth_gate = build_auth_gate(settings.auth)

    # Cross-cutting. Order matters: correlation runs outermost so every log line
    # and every error body carries the request id. (P2 adds the admission limiter
    # inside it, so rejections are still correlated.)
    app.add_middleware(CorrelationMiddleware)

    # Error translation as a table, not a per-endpoint decorator (doc 08 §8.3).
    register_exception_handlers(app)

    # Unversioned operational surface — probes must not be pinned to a version.
    app.include_router(ops.router)

    # Versioned surface: every enabled entry in api/versions.py.
    for _name, router in versions.mounted(settings):
        app.include_router(router)

    return app


__all__ = ["create_app"]
