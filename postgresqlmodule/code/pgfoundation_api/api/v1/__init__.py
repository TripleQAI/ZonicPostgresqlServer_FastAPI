"""v1 — the stable contract.

``build(settings)`` assembles this version's router: the generated data-plane
routes plus the hand-written admin routes. The engine that generates them lives
in ``registry/`` and knows nothing about v1 (doc 09 §9.1b).
"""

from __future__ import annotations

from fastapi import APIRouter

from ...registry.router import build_router
from ...settings import ServiceSettings
from .operations import OPERATION_BY_ID, OPERATIONS
from .routes import admin, batch

PREFIX = "/v1"


def build(settings: ServiceSettings) -> APIRouter:
    """Assemble the v1 surface."""
    router = APIRouter()
    router.include_router(build_router(OPERATIONS, prefix=PREFIX, settings=settings))
    router.include_router(admin.router, prefix=PREFIX)
    if settings.batch.enabled:
        router.include_router(batch.router, prefix=PREFIX)
    return router


__all__ = ["OPERATION_BY_ID", "OPERATIONS", "PREFIX", "build"]
