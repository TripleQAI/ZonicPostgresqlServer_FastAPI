"""P5.4/P5.5/P5.6 — the OBSERVE markers, made real.

Doc 11 is a contract with the external observability project: signal names,
labels, redaction rules and a cardinality budget. These tests hold the emission
side of it, so the contract is checked rather than described.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.observability.metrics import MetricsCollector
from pgfoundation_api.settings import ServiceSettings

ON = {"observability": {"metrics_enabled": True}}


async def build(extra: dict | None = None, names: tuple[str, ...] = ("main",)):
    settings = ServiceSettings(**{**ON, **(extra or {})})
    app_settings = load_settings(
        {"datasources": [{"name": n, "dsn": f"postgresql://x/{n}"} for n in names]}
    )
    foundation = await DataFoundation.from_settings(
        app_settings,
        driver=FakeDriver(lambda spec: ResultSet(rows=[{"id": 1}], rowcount=1, elapsed_ms=1.0)),
    )
    return create_app(settings, foundation=foundation), foundation


def client_for(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# --------------------------------------------------------------------------- #
# The port contract (ADR-014)
# --------------------------------------------------------------------------- #
def test_metrics_are_a_noop_by_default():
    """Zero cost until a deployment turns them on."""
    app = create_app(ServiceSettings())
    assert not hasattr(app.state.metrics, "render")


async def test_metrics_endpoint_renders_prometheus_text():
    app, foundation = await build()
    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post("/v1/datasources/main/view", json={"name": "v"})
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE pgf_http_requests_total counter" in body
    assert "pgf_http_request_duration_seconds_bucket" in body
    await foundation.aclose()


async def test_metrics_endpoint_is_empty_when_disabled():
    """Absent signals are the default, not an error."""
    app = create_app(ServiceSettings())
    async with client_for(app) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.text == ""


# --------------------------------------------------------------------------- #
# The cardinality budget (doc 11 §11.9)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["request_id", "sql", "subject"])
def test_forbidden_labels_raise_rather_than_minting_series(bad):
    """One unbounded label can cost more than the service it measures."""
    collector = MetricsCollector()
    with pytest.raises(ValueError, match="unbounded"):
        collector.incr("pgf_test_total", 1, **{bad: "anything"})


def test_undeclared_labels_are_rejected():
    collector = MetricsCollector()
    with pytest.raises(ValueError, match="unknown metric label"):
        collector.incr("pgf_test_total", 1, invented="x")


def test_out_of_range_enum_values_fold_into_other():
    """Fold, do not mint: an unexpected value must not create a new series."""
    collector = MetricsCollector()
    collector.incr("pgf_http_requests_total", 1, method="PROPFIND")
    assert collector.counter_value("pgf_http_requests_total", method="other") == 1


async def test_route_label_is_the_template_not_the_concrete_path():
    """`/v1/datasources/{name}/view`, never `/v1/datasources/main/view`.

    The concrete path embeds the data-source name, so it would mint one series
    per source per route — the classic way a metrics bill outgrows the service.
    """
    app, foundation = await build(names=("main", "analytics"))
    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post("/v1/datasources/main/view", json={"name": "v"})
        await client.post("/v1/datasources/analytics/view", json={"name": "v"})
        body = (await client.get("/metrics")).text

    assert "{name}" in body
    assert 'route="/v1/datasources/main/view"' not in body
    await foundation.aclose()


async def test_series_count_stays_bounded_across_many_datasources():
    app, foundation = await build(names=tuple(f"ds{i}" for i in range(8)))
    async with app.router.lifespan_context(app), client_for(app) as client:
        for i in range(8):
            await client.post(f"/v1/datasources/ds{i}/view", json={"name": "v"})

    # Eight data sources through ONE route template must not produce 8x series.
    assert app.state.metrics.series_count() < 30
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# The signals that prove requirement 4
# --------------------------------------------------------------------------- #
async def test_batch_emits_the_concurrency_peak():
    """The signal that proves the fan-out is real (doc 11 §11.3).

    No latency metric would reveal a fan-out that silently serialized.
    """
    app, foundation = await build()
    items = [
        {"id": f"i{k}", "datasource": "main", "op": "view", "payload": {"name": "v"}}
        for k in range(5)
    ]
    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post("/v1/batch", json={"mode": "parallel", "items": items})

    collector = app.state.metrics
    assert collector.counter_value("pgf_batch_requests_total", mode="parallel") == 1
    assert collector.counter_value("pgf_batch_items_total", mode="parallel", status="ok") == 5
    assert collector.histogram_count("pgf_batch_duration_seconds", mode="parallel") == 1
    assert collector.gauge_value("pgf_batch_concurrency_peak", mode="parallel") >= 1
    await foundation.aclose()


async def test_admission_wait_is_timed_on_the_rejection_path_too():
    """Waiting-then-rejected is the signal that says the cap is too low.

    Timing only the successful acquires hides it completely.
    """
    app, foundation = await build(
        {
            "batch": {"max_concurrency": 4},
            "concurrency": {
                "max_inflight_total": 4,
                "max_inflight_per_datasource": 1,
                "admission_timeout_ms": 30,
            },
        }
    )
    items = [
        {"id": f"i{k}", "datasource": "main", "op": "view", "payload": {"name": "v"}}
        for k in range(4)
    ]
    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post("/v1/batch", json={"mode": "parallel", "items": items})

    waits = app.state.metrics.histogram_count(
        "pgf_admission_wait_seconds", scope="global", datasource="main"
    )
    assert waits >= 4
    await foundation.aclose()


async def test_auth_outcomes_are_counted_without_the_subject():
    """`subject` is a log field, never a metric label (doc 11 §11.9)."""
    from pgfoundation_api.security.gate import build_auth_gate
    from pgfoundation_api.settings import AuthSettings

    app, foundation = await build()
    app.state.auth_gate = build_auth_gate(
        AuthSettings.model_validate(
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
        ),
        metrics=app.state.metrics,
    )

    header = {"x-client-cert-subject": "orders-api"}
    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post("/v1/datasources/main/view", json={"name": "v"}, headers=header)
        await client.post(
            "/v1/datasources/main/procedure", json={"name": "p", "args": []}, headers=header
        )
        await client.post("/v1/datasources/main/view", json={"name": "v"})
        body = (await client.get("/metrics")).text

    collector = app.state.metrics
    assert collector.counter_value("pgf_auth_checks_total", action="read", result="allow") == 1
    assert collector.counter_value("pgf_auth_checks_total", action="write", result="deny") == 1
    assert (
        collector.counter_value("pgf_auth_checks_total", action="read", result="unauthenticated")
        == 1
    )
    assert "orders-api" not in body, "the subject must not reach a metric label"
    await foundation.aclose()


async def test_no_credential_reaches_the_metrics_endpoint():
    """Scrape output is as public as the OpenAPI document (doc 11 §11.8)."""
    settings = ServiceSettings(**ON)
    app_settings = load_settings(
        {"datasources": [{"name": "main", "dsn": "postgresql://u:hunter2@h/db"}]}
    )
    foundation = await DataFoundation.from_settings(app_settings, driver=FakeDriver())
    app = create_app(settings, foundation=foundation)

    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post("/v1/datasources/main/view", json={"name": "v"})
        body = (await client.get("/metrics")).text

    for secret in ("hunter2", "postgresql://"):
        assert secret not in body
    await foundation.aclose()
