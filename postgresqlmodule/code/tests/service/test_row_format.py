"""P5.1 — selectable row format (ADR-007, doc 07 §7.5).

`dict` stays the default so no existing client changes. `tuple` skips building
one mapping per row and drops the repeated keys from the payload, which is where
the saving actually is on wide results.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

VIEW = {"name": "v_orders"}


def responder(spec):
    return ResultSet(
        rows=[{"id": 1, "customer": "alice"}, {"id": 2, "customer": "bob"}],
        rowcount=2,
        elapsed_ms=1.0,
    )


async def build(settings: ServiceSettings):
    app_settings = load_settings({"datasources": [{"name": "main", "dsn": "postgresql://x/m"}]})
    foundation = await DataFoundation.from_settings(app_settings, driver=FakeDriver(responder))
    return create_app(settings, foundation=foundation), foundation


async def call(app, payload):
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        return (await client.post("/v1/datasources/main/view", json=payload)).json()


async def test_dict_is_the_default_and_omits_columns():
    """Existing clients must see exactly what they saw before."""
    app, foundation = await build(ServiceSettings())
    body = await call(app, VIEW)

    assert body["rows"] == [{"id": 1, "customer": "alice"}, {"id": 2, "customer": "bob"}]
    assert body.get("columns") is None
    await foundation.aclose()


async def test_tuple_format_returns_arrays_plus_columns():
    app, foundation = await build(ServiceSettings())
    body = await call(app, {**VIEW, "row_format": "tuple"})

    assert body["columns"] == ["id", "customer"]
    assert body["rows"] == [[1, "alice"], [2, "bob"]]
    assert body["row_count"] == 2
    await foundation.aclose()


async def test_tuple_payload_is_smaller_than_dict():
    """The point of the format: column names appear once, not per row."""
    app, foundation = await build(ServiceSettings())
    as_dict = await call(app, VIEW)
    as_tuple = await call(app, {**VIEW, "row_format": "tuple"})

    assert len(str(as_tuple["rows"])) < len(str(as_dict["rows"]))
    await foundation.aclose()


async def test_config_default_applies_without_any_client_change():
    """A deployment can move its whole surface to tuples via config alone."""
    app, foundation = await build(ServiceSettings(api={"default_row_format": "tuple"}))
    body = await call(app, VIEW)  # no row_format in the request

    assert body["columns"] == ["id", "customer"]
    assert body["rows"] == [[1, "alice"], [2, "bob"]]
    await foundation.aclose()


async def test_request_overrides_the_configured_default():
    app, foundation = await build(ServiceSettings(api={"default_row_format": "tuple"}))
    body = await call(app, {**VIEW, "row_format": "dict"})

    assert body.get("columns") is None
    assert body["rows"][0] == {"id": 1, "customer": "alice"}
    await foundation.aclose()


@pytest.mark.parametrize("bad", ["arrow", "list", ""])
async def test_unknown_row_format_is_rejected(bad):
    """A closed enum, so an unknown value never reaches the driver."""
    app, foundation = await build(ServiceSettings())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post("/v1/datasources/main/view", json={**VIEW, "row_format": bad})
    assert response.status_code == 400
    await foundation.aclose()


async def test_row_format_is_documented_in_openapi():
    app = create_app(ServiceSettings())
    schema = app.openapi()["components"]["schemas"]["ViewIn"]["properties"]
    assert "row_format" in schema
