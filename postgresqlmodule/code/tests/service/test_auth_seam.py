"""P1.12 — the auth seam (ADR-008), ported from the Django service tests.

The behaviour is unchanged: disabled by default, fails closed when enabled with
no provider. What changed is that the check is a **dependency supplied by the
descriptor**, so an endpoint cannot be registered without it.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.security.gate import AuthGate
from pgfoundation_api.security.ports import Principal

VIEW = {"name": "v_paid_orders"}


async def _foundation() -> DataFoundation:
    s = load_settings({"datasources": [{"name": "main", "dsn": "postgresql://x/main"}]})
    return await DataFoundation.from_settings(
        s, driver=FakeDriver(lambda spec: ResultSet(rows=[{"id": 1}], rowcount=1))
    )


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_auth_disabled_by_default_allows(settings):
    foundation = await _foundation()
    app = create_app(settings, foundation=foundation)
    async with app.router.lifespan_context(app), await _client(app) as c:
        assert (await c.post("/v1/datasources/main/view", json=VIEW)).status_code == 200
    await foundation.aclose()


async def test_authorizer_denial_maps_to_403(settings):
    class DenyAll:
        async def authorize(self, principal, action, resource):
            return False

    class Anyone:
        async def authenticate(self, request):
            return Principal(subject="caller")

    foundation = await _foundation()
    app = create_app(settings, foundation=foundation)
    app.state.auth_gate = AuthGate(enabled=True, authenticator=Anyone(), authorizer=DenyAll())

    async with app.router.lifespan_context(app), await _client(app) as c:
        response = await c.post("/v1/datasources/main/view", json=VIEW)
    assert response.status_code == 403
    assert response.json()["type"] == "AuthorizationError"
    await foundation.aclose()


async def test_unauthenticated_maps_to_401(settings):
    class NoOne:
        async def authenticate(self, request):
            return None

    class AllowAll:
        async def authorize(self, principal, action, resource):
            return True

    foundation = await _foundation()
    app = create_app(settings, foundation=foundation)
    app.state.auth_gate = AuthGate(enabled=True, authenticator=NoOne(), authorizer=AllowAll())

    async with app.router.lifespan_context(app), await _client(app) as c:
        response = await c.post("/v1/datasources/main/view", json=VIEW)
    assert response.status_code == 401
    await foundation.aclose()


def test_auth_enabled_without_provider_fails_closed():
    """Never silently open — the behaviour the prod profile depends on."""
    with pytest.raises(RuntimeError, match="refusing to start"):
        AuthGate(enabled=True)


async def test_authorizer_receives_datasource_as_the_resource(settings):
    """ADR-008 scopes policy to `caller -> {datasource} x {action}`."""
    seen: list[tuple[str, str]] = []

    class Recording:
        async def authorize(self, principal, action, resource):
            seen.append((action, resource))
            return True

    class Anyone:
        async def authenticate(self, request):
            return Principal(subject="orders-api")

    foundation = await _foundation()
    app = create_app(settings, foundation=foundation)
    app.state.auth_gate = AuthGate(enabled=True, authenticator=Anyone(), authorizer=Recording())

    async with app.router.lifespan_context(app), await _client(app) as c:
        await c.post("/v1/datasources/main/view", json=VIEW)
        await c.post("/v1/datasources/main/procedure", json={"name": "sp_x", "args": []})

    assert seen == [("read", "main"), ("write", "main")]  # action comes from the descriptor
    await foundation.aclose()
