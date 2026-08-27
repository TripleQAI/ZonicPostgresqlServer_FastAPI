"""P1.4 — the walking skeleton (doc 13 §13.4).

The service boots, the lifespan builds the foundation on the serving loop, and the
unversioned operational surface answers. No data-plane code exists yet.
"""

from __future__ import annotations

import pytest

from pgfoundation import DataFoundation, HealthStatus
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.ops import roll_up
from pgfoundation_api.settings import ServiceSettings, load_service_settings

# --- the skeleton itself ---------------------------------------------------- #


async def test_health_reports_healthy(client):
    """Liveness never queries the database (2026-08-27 fix) — ``datasources``
    lists known names without probing them, so it reads "unknown" here even
    though the fake driver would report "healthy" if asked. That's the point:
    a slow/cold database must not make liveness slow too.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["datasources"] == {"orders": "unknown"}


async def test_ready_reports_healthy(client):
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


async def test_ops_endpoints_are_unversioned(client):
    """Probes must not be pinned to an API version (doc 09 §9.1b).

    The unversioned pair is what probes target; the ``/v1`` aliases exist only
    for parity with the Django surface and report the same thing.
    """
    for path in ("/health", "/ready", "/v1/health", "/v1/ready"):
        response = await client.get(path)
        assert response.status_code == 200, path
    assert (await client.get("/health")).json() == (await client.get("/v1/health")).json()


async def test_openapi_is_generated(client):
    """FastAPI generates it — the hand-written apispec builder is gone (ADR-001)."""
    spec = (await client.get("/openapi.json")).json()
    assert spec["info"]["title"] == "pgfoundation-service"
    assert "/health" in spec["paths"]
    assert "/ready" in spec["paths"]


# --- health roll-up: three states, not two ---------------------------------- #


@pytest.mark.parametrize(
    ("per_source", "expected"),
    [
        ({}, "down"),
        ({"a": HealthStatus.HEALTHY}, "healthy"),
        ({"a": HealthStatus.HEALTHY, "b": HealthStatus.HEALTHY}, "healthy"),
        ({"a": HealthStatus.HEALTHY, "b": HealthStatus.DOWN}, "degraded"),
        ({"a": HealthStatus.DOWN}, "down"),
        ({"a": HealthStatus.DOWN, "b": HealthStatus.DOWN}, "down"),
        ({"a": HealthStatus.DEGRADED}, "down"),  # degraded is not healthy
    ],
)
def test_roll_up_is_tri_state(per_source, expected):
    """The Django shell could never report `down`; this one can (doc 06 §6.4)."""
    assert roll_up(per_source) == expected


async def test_ready_returns_503_when_all_sources_down(make_foundation, settings):
    from httpx import ASGITransport, AsyncClient

    app_settings = load_settings(
        {"datasources": [{"name": "orders", "dsn": "postgresql://x/orders"}]}
    )
    driver = FakeDriver()
    foundation = await DataFoundation.from_settings(app_settings, driver=driver)
    for pool in driver.pools.values():
        pool.saturated = True  # FakePool reports DOWN when saturated

    app = create_app(settings, foundation=foundation)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        response = await ac.get("/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "down"

        # Liveness must stay 200: the process is fine, the database is not.
        assert (await ac.get("/health")).status_code == 200
    await foundation.aclose()


# --- wiring: no globals, per-app isolation (ADR-005) ------------------------ #


async def test_two_apps_coexist_with_separate_foundations(make_foundation, settings):
    """The Django shell's module-global `_foundation` made this impossible."""
    first = await make_foundation(names=("orders",))
    second = await make_foundation(names=("analytics", "reporting"))

    app_a = create_app(settings, foundation=first)
    app_b = create_app(settings, foundation=second)

    assert app_a.state.foundation is not app_b.state.foundation
    assert app_a.state.foundation.names() == ["orders"]
    assert app_b.state.foundation.names() == ["analytics", "reporting"]


async def test_injected_foundation_is_not_closed_by_lifespan(make_foundation, settings):
    """Whoever injects a foundation owns its lifecycle."""
    foundation = await make_foundation()
    app = create_app(settings, foundation=foundation)
    async with app.router.lifespan_context(app):
        pass
    assert app.state.owns_foundation is False
    # Still usable after the app shut down.
    assert foundation.names() == ["orders"]


# --- settings (P1.1) -------------------------------------------------------- #


def test_defaults_are_documented_not_magic():
    s = ServiceSettings()
    assert s.server.port == 8600
    assert s.api.enable_raw_sql is False  # trusted surface off by default
    assert s.auth.enabled is False  # ADR-008: on in the prod profile
    assert s.batch.default_mode == "parallel"


def test_env_overrides_nested_settings(monkeypatch):
    monkeypatch.setenv("PGF_SERVER__PORT", "9999")
    monkeypatch.setenv("PGF_BATCH__MAX_ITEMS", "7")
    s = ServiceSettings()
    assert s.server.port == 9999
    assert s.batch.max_items == 7


def test_profile_overlay_merges_over_base(tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "base.yaml").write_text(
        "api:\n  docs_enabled: true\n  max_response_rows: 10\n", encoding="utf-8"
    )
    (tmp_path / "profiles" / "prod.yaml").write_text(
        "api:\n  docs_enabled: false\n", encoding="utf-8"
    )
    s = load_service_settings(tmp_path / "base.yaml", profile="prod")
    assert s.api.docs_enabled is False  # overlay wins
    assert s.api.max_response_rows == 10  # untouched keys survive the merge


def test_raw_sql_without_auth_is_refused_outside_dev():
    """The one combination that must never ship (doc 05 §5.5, ADR-008)."""
    from pgfoundation import ConfigError

    with pytest.raises(ConfigError, match="enable_raw_sql"):
        load_service_settings(
            None, profile="prod", api={"enable_raw_sql": True}, auth={"enabled": False}
        )

    # dev may opt in
    s = load_service_settings(
        None, profile="dev", api={"enable_raw_sql": True}, auth={"enabled": False}
    )
    assert s.api.enable_raw_sql is True


def test_batch_concurrency_cannot_exceed_global_admission_cap():
    from pgfoundation import ConfigError

    with pytest.raises(ConfigError, match="max_concurrency"):
        load_service_settings(
            None, batch={"max_concurrency": 999}, concurrency={"max_inflight_total": 8}
        )


def test_auth_enabled_with_deny_all_and_no_callers_is_refused():
    """Authorizing nobody is a misconfiguration, not a lockdown (ADR-008)."""
    from pgfoundation import ConfigError

    with pytest.raises(ConfigError, match="authorizes nobody"):
        load_service_settings(None, auth={"enabled": True, "provider": "mtls"})
