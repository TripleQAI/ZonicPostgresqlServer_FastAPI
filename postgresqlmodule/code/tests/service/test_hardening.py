"""P4.12/P4.13 — hardening, caller auth, and the redaction suite.

Covers the concurrency defects closed in P4, the ADR-008 auth boundary, and the
rule that no credential may appear in any signal the service emits.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.clock import ManualClock
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation._internal.resilience.circuit_breaker import CircuitBreaker, CircuitState
from pgfoundation_api.app import create_app
from pgfoundation_api.security.gate import AuthGate, build_auth_gate
from pgfoundation_api.security.policy import PolicyAuthorizer
from pgfoundation_api.security.ports import Principal
from pgfoundation_api.settings import AuthPolicySettings, AuthSettings, ServiceSettings

DSN = "postgresql://secret_user:hunter2@db.internal:5432/orders"


# --------------------------------------------------------------------------- #
# P4.1 — exactly one probe in HALF_OPEN
# --------------------------------------------------------------------------- #
def test_half_open_admits_exactly_one_probe():
    """The stampede the breaker exists to prevent.

    `allow()` and the result are separated by an `await` in the caller, so every
    coroutine that arrives during a probe used to be admitted too — sending the
    whole backlog at a database just declared unhealthy.
    """
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=10, clock=clock)

    breaker.on_failure()
    breaker.on_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False

    clock._t += 11  # past the reset window -> HALF_OPEN (ManualClock advances by assignment)
    assert breaker.allow() is True, "the first probe must be admitted"
    assert breaker.allow() is False, "a second concurrent probe must NOT be"
    assert breaker.allow() is False


def test_probe_success_closes_and_reopens_the_gate():
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=5, clock=clock)
    breaker.on_failure()
    clock._t += 6  # past the reset window

    assert breaker.allow() is True
    breaker.on_success()
    assert breaker.state is CircuitState.CLOSED
    # Closed again: concurrent callers are all admitted.
    assert breaker.allow() is True
    assert breaker.allow() is True


def test_probe_failure_reopens_and_blocks_again():
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=5, clock=clock)
    breaker.on_failure()
    clock._t += 6  # past the reset window

    assert breaker.allow() is True
    breaker.on_failure()  # the probe failed
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False


# --------------------------------------------------------------------------- #
# P4.2 — explicit unwrap contract
# --------------------------------------------------------------------------- #
async def test_metered_pool_releases_the_drivers_object():
    """The pool must hand the driver's object back, not the decorator."""
    driver = FakeDriver()
    settings = load_settings({"datasources": [{"name": "main", "dsn": DSN}]})
    foundation = await DataFoundation.from_settings(settings, driver=driver)

    await foundation.query("main", __import__("pgfoundation").Query("SELECT 1"))
    pool = next(iter(driver.pools.values()))
    assert pool.released == pool.acquired
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# P4.8 / P4.9 — caller auth and coarse policy (ADR-008)
# --------------------------------------------------------------------------- #
async def test_policy_scopes_a_caller_to_its_datasources_and_actions():
    policy = PolicyAuthorizer(
        AuthPolicySettings.model_validate(
            {
                "default": "deny",
                "callers": [
                    {"subject": "analytics-api", "datasources": ["analytics"], "actions": ["read"]},
                    {
                        "subject": "orders-api",
                        "datasources": ["main"],
                        "actions": ["read", "write"],
                    },
                ],
            }
        )
    )
    analytics = Principal(subject="analytics-api")
    orders = Principal(subject="orders-api")

    assert await policy.authorize(analytics, "read", "analytics") is True
    # The whole point: read-only on its own source, nothing on anyone else's.
    assert await policy.authorize(analytics, "write", "analytics") is False
    assert await policy.authorize(analytics, "read", "main") is False
    assert await policy.authorize(orders, "write", "main") is True
    # An unknown caller is not a partially-trusted caller.
    assert await policy.authorize(Principal(subject="who?"), "read", "main") is False


async def test_mesh_identity_authenticates_from_the_header():
    settings = AuthSettings.model_validate(
        {
            "enabled": True,
            "provider": "mtls",
            "policy": {
                "default": "deny",
                "callers": [
                    {"subject": "orders-api", "datasources": ["main"], "actions": ["read"]}
                ],
            },
        }
    )
    gate = build_auth_gate(settings)

    driver = FakeDriver(lambda spec: ResultSet(rows=[{"id": 1}], rowcount=1))
    app_settings = load_settings({"datasources": [{"name": "main", "dsn": DSN}]})
    foundation = await DataFoundation.from_settings(app_settings, driver=driver)
    app = create_app(ServiceSettings(), foundation=foundation)
    app.state.auth_gate = gate

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        # No identity header -> 401
        anon = await client.post("/v1/datasources/main/view", json={"name": "v"})
        assert anon.status_code == 401

        # Identified and permitted -> 200
        ok = await client.post(
            "/v1/datasources/main/view",
            json={"name": "v"},
            headers={"x-client-cert-subject": "orders-api"},
        )
        assert ok.status_code == 200

        # Identified but not permitted for this action -> 403
        denied = await client.post(
            "/v1/datasources/main/procedure",
            json={"name": "sp", "args": []},
            headers={"x-client-cert-subject": "orders-api"},
        )
        assert denied.status_code == 403
    await foundation.aclose()


def test_unknown_provider_fails_closed():
    """A typo in a prod profile must not silently fall back to allow-all."""
    settings = AuthSettings.model_validate(
        {
            "enabled": True,
            "provider": "oauth-typo",
            "policy": {"default": "deny", "callers": [{"subject": "x", "actions": ["read"]}]},
        }
    )
    with pytest.raises(RuntimeError, match="refusing to start"):
        build_auth_gate(settings)


def test_auth_disabled_needs_no_provider():
    gate = build_auth_gate(AuthSettings())
    assert gate.enabled is False


def test_enabled_without_any_provider_still_fails_closed():
    with pytest.raises(RuntimeError, match="refusing to start"):
        AuthGate(enabled=True)


# --------------------------------------------------------------------------- #
# P4.13 — redaction: no credential in any emitted signal (doc 11 §11.8)
# --------------------------------------------------------------------------- #
async def test_no_credential_appears_in_any_error_body():
    driver = FakeDriver()
    app_settings = load_settings({"datasources": [{"name": "main", "dsn": DSN}]})
    foundation = await DataFoundation.from_settings(app_settings, driver=driver)
    app = create_app(ServiceSettings(), foundation=foundation)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        bodies = [
            (await client.post("/v1/datasources/main/view", json={"name": "x; DROP"})).text,
            (await client.post("/v1/datasources/nope/view", json={"name": "v"})).text,
            (await client.post("/v1/datasources/main/view", json={"bad": 1})).text,
            (await client.get("/v1/datasources")).text,
            (await client.get("/health")).text,
        ]

    for body in bodies:
        for secret in ("hunter2", "secret_user", "db.internal", "postgresql://"):
            assert secret not in body, f"{secret!r} leaked into a response body"
    await foundation.aclose()


def test_dsn_is_not_in_the_settings_repr():
    settings = load_settings({"datasources": [{"name": "main", "dsn": DSN}]})
    text = repr(settings) + str(settings.model_dump())
    assert "hunter2" not in text
    assert "SecretStr" in repr(settings.datasources[0].dsn) or "**" in repr(
        settings.datasources[0].dsn
    )


async def test_openapi_document_contains_no_credentials():
    """The spec is public; a DSN in an example or default would be published."""
    app = create_app(ServiceSettings(profile="dev", api={"enable_raw_sql": True}))
    text = str(app.openapi())
    for secret in ("hunter2", "secret_user", "postgresql://"):
        assert secret not in text


# --------------------------------------------------------------------------- #
# P4.13 — metric label cardinality (doc 11 §11.9)
# --------------------------------------------------------------------------- #
def test_policy_subjects_are_a_bounded_set():
    """`subject` is safe as a log field because policy enumerates it in config.

    It is NOT safe as a metric label unless bounded — this asserts the bound
    exists, which is what makes the doc 11 §11.9 guidance actionable.
    """
    policy = PolicyAuthorizer(
        AuthPolicySettings.model_validate(
            {"default": "deny", "callers": [{"subject": "a"}, {"subject": "b"}]}
        )
    )
    assert policy.known_subjects() == {"a", "b"}
