"""P1.18/P1.19 — parity harness + the frozen wire contracts.

The parity harness is the single most valuable test in this port: for each
operation it runs the *same* logical call through **library mode** and **service
mode** and asserts they agree. That is what makes "the shell holds no data-access
logic" a checked property rather than a claim (doc 10 §10.4).

The frozen-contract tests pin the behaviours clients already depend on
(doc 08 §8.2). Each one exists because it is a plausible silent regression.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.api.v1 import PREFIX
from pgfoundation_api.api.v1.operations import OPERATIONS
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

#: One representative payload per operation.
PAYLOADS: dict[str, dict] = {
    "view": {
        "name": "v_paid_orders",
        "columns": ["id", "customer", "amount"],
        "filters": [{"column": "customer", "op": "eq", "value": "bob"}],
        "order_by": [{"column": "id"}],
    },
    "function": {"name": "fn_orders_by_customer", "args": {"cust": "bob"}},
    "procedure": {"name": "sp_customer_stats", "args": ["bob", None, None]},
    "query": {"sql": "SELECT id FROM demo_orders WHERE id = %(i)s", "params": {"i": 1}},
    "execute": {"sql": "UPDATE demo_orders SET status = %(s)s", "params": {"s": "paid"}},
    "interval": {
        "source": "readings",
        "time_column": "ts",
        "start": "2026-08-13T00:00:00Z",
        "end": "2026-08-14T00:00:00Z",
        "every": "15 minutes",
        "metrics": [{"column": "amount", "func": "sum"}],
        "group_by": ["customer"],
    },
    # order_by is ["id"] because the watermark reads that column off the last
    # row, and the fake responder returns rows keyed id/customer/amount.
    "fetch_since": {
        "sql": "SELECT id, customer FROM demo_orders",
        "params": {},
        "cursor": {"order_by": ["id"]},
        "limit": 10,
    },
    "execute_many": {
        "sql": "INSERT INTO demo_orders (id) VALUES (%(id)s)",
        "params_seq": [{"id": 10}, {"id": 11}],
    },
    "copy": {"table": "demo_orders", "rows": [[1, "alice"], [2, "bob"]]},
}


def responder(spec) -> ResultSet:
    """Deterministic, and deliberately returns a Decimal (the frozen contract)."""
    if spec.sql.strip().upper().startswith(("SELECT", "CALL")):
        return ResultSet(
            rows=[{"id": 3, "customer": "bob", "amount": Decimal("42.00")}],
            rowcount=1,
            elapsed_ms=1.25,
        )
    return ResultSet(rows=[], rowcount=2, elapsed_ms=0.5)


@pytest.fixture
def settings() -> ServiceSettings:
    # Raw SQL on so query/execute are exercised too; dev profile permits it.
    return ServiceSettings(profile="dev", api={"enable_raw_sql": True})


async def _foundation() -> DataFoundation:
    app_settings = load_settings({"datasources": [{"name": "main", "dsn": "postgresql://x/main"}]})
    return await DataFoundation.from_settings(app_settings, driver=FakeDriver(responder))


# --------------------------------------------------------------------------- #
# Parity: library mode vs service mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec", OPERATIONS, ids=lambda s: s.op)
async def test_library_and_service_modes_agree(spec, settings):
    foundation = await _foundation()
    app = create_app(settings, foundation=foundation)

    # --- library mode: straight through the Facade ---
    dto = spec.request_model.model_validate(PAYLOADS[spec.op])
    handle = foundation.datasource("main")
    lib_result = await spec.call(handle, dto)
    lib_body = spec.to_response(lib_result).model_dump(mode="json")

    # --- service mode: over HTTP ---
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(
            f"{PREFIX}/datasources/main/{spec.suffix}", json=PAYLOADS[spec.op]
        )

    assert response.status_code == 200, response.text
    assert response.json() == lib_body, f"{spec.op}: shell changed the result"
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# Frozen wire contracts (doc 08 §8.2)
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(settings):
    foundation = await _foundation()
    app = create_app(settings, foundation=foundation)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac,
    ):
        yield ac
    await foundation.aclose()


async def test_numeric_serializes_as_string(client):
    """ADR-007's non-negotiable rule.

    `numeric` must stay an exact JSON string. A float here silently corrupts
    money, and every test that only checks the value would still pass.
    """
    response = await client.post(f"{PREFIX}/datasources/main/view", json=PAYLOADS["view"])
    amount = response.json()["rows"][0]["amount"]
    assert amount == "42.00"
    assert isinstance(amount, str), f"numeric became {type(amount).__name__}"


async def test_response_envelope_field_names(client):
    body = (await client.post(f"{PREFIX}/datasources/main/view", json=PAYLOADS["view"])).json()
    assert set(body) == {"rows", "row_count", "elapsed_ms"}
    assert isinstance(body["row_count"], int)
    assert isinstance(body["elapsed_ms"], float)


async def test_schema_alias_is_accepted(client):
    """The request field is `schema`, not `db_schema`."""
    response = await client.post(
        f"{PREFIX}/datasources/main/view", json={"name": "v_paid_orders", "schema": "public"}
    )
    assert response.status_code == 200


async def test_injection_in_object_name_is_rejected(client):
    """The compilers are the security boundary; the shell must not soften them."""
    response = await client.post(
        f"{PREFIX}/datasources/main/view",
        json={"name": "v_paid_orders; DROP TABLE demo_orders"},
    )
    assert response.status_code == 400
    assert response.json()["type"] == "QueryError"


async def test_unknown_datasource_maps_to_404(client):
    response = await client.post(f"{PREFIX}/datasources/nope/view", json=PAYLOADS["view"])
    assert response.status_code == 404


async def test_invalid_body_maps_to_400_without_leaking_fields(client):
    response = await client.post(f"{PREFIX}/datasources/main/view", json={"wrong": 1})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid request body"  # generic, by design
    assert "wrong" not in str(body)  # no field internals echoed


async def test_error_body_leaks_no_internals(client):
    body = (
        await client.post(f"{PREFIX}/datasources/main/view", json={"name": "bad; DROP TABLE x"})
    ).json()
    assert set(body) == {"error", "type", "request_id"}
    text = str(body).lower()
    for secret in ("dsn", "password", "postgresql://", "traceback"):
        assert secret not in text


async def test_request_id_is_echoed_when_supplied(client):
    response = await client.get("/v1/datasources", headers={"X-Request-Id": "abc123"})
    assert response.headers["X-Request-Id"] == "abc123"


async def test_request_id_is_generated_when_absent(client):
    response = await client.get("/v1/datasources")
    assert response.headers.get("X-Request-Id")


async def test_error_responses_also_carry_the_request_id(client):
    response = await client.post(
        f"{PREFIX}/datasources/main/view",
        json={"name": "bad; DROP"},
        headers={"X-Request-Id": "trace-me"},
    )
    assert response.status_code == 400
    assert response.json()["request_id"] == "trace-me"


async def test_datasources_listing(client):
    body = (await client.get("/v1/datasources")).json()
    assert body == {"datasources": ["main"]}


async def test_filter_operator_whitelist_is_enforced(client):
    """`op` is a closed enum — an unknown operator never reaches SQL."""
    response = await client.post(
        f"{PREFIX}/datasources/main/view",
        json={"name": "v", "filters": [{"column": "c", "op": "; DROP", "value": 1}]},
    )
    assert response.status_code == 400
