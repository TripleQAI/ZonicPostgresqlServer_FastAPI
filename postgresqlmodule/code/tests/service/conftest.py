"""Service-test fixtures: the whole shell over ASGI, with no database.

Replaces the Django shell's ``django.setup()`` + ``AsyncRequestFactory`` +
module-global ``bootstrap.set_foundation()``. Each test builds its **own** app
with an injected fake-driver foundation, so there is no global state to reset and
tests cannot leak into one another (ADR-005).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings


def default_responder(spec) -> ResultSet:
    """Deterministic canned results, matching the Django suite's fixture."""
    sql = spec.sql.strip().upper()
    if sql.startswith("SELECT"):
        return ResultSet(rows=[{"id": 1, "total": "9.90"}], rowcount=1, elapsed_ms=1.5)
    return ResultSet(rows=[], rowcount=2, elapsed_ms=0.8)


@pytest.fixture
def settings() -> ServiceSettings:
    return ServiceSettings()


@pytest_asyncio.fixture
async def make_foundation() -> AsyncIterator[Callable[..., object]]:
    """Factory for fake-driver foundations; every one built is closed on teardown."""
    built: list[DataFoundation] = []

    async def _make(responder=default_responder, names: tuple[str, ...] = ("orders",)):
        app_settings = load_settings(
            {"datasources": [{"name": n, "dsn": f"postgresql://x/{n}"} for n in names]}
        )
        foundation = await DataFoundation.from_settings(app_settings, driver=FakeDriver(responder))
        built.append(foundation)
        return foundation

    yield _make
    for foundation in built:
        await foundation.aclose()


@pytest_asyncio.fixture
async def client(make_foundation, settings) -> AsyncIterator[AsyncClient]:
    """An httpx client bound to a fully-wired app (lifespan included)."""
    foundation = await make_foundation()
    app = create_app(settings, foundation=foundation)
    transport = ASGITransport(app=app)
    # Lifespan first, so startup has run before any request — and so startup/shutdown
    # logic is exercised by every service test rather than bypassed.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as ac,
    ):
        yield ac
