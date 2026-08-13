"""Phase 0 walking skeleton — end-to-end query through the Facade, no DB.

Uses the in-memory FakeDriver injected at the composition root (doc 13 §13.2).
"""
from __future__ import annotations

import pytest

from pgfoundation import (
    Command,
    ConfigError,
    DataFoundation,
    HealthStatus,
    Query,
    ResultSet,
)
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver


def _make(responder=None, names=("orders",)):
    cfg = {"datasources": [{"name": n, "dsn": f"postgresql://x/{n}"} for n in names]}
    settings = load_settings(cfg)
    return settings, FakeDriver(responder)


async def test_end_to_end_query_via_facade():
    settings, driver = _make()
    async with await DataFoundation.from_settings(settings, driver=driver) as db:
        rs = await db.query("orders", Query("SELECT 1"))
        assert isinstance(rs, ResultSet)
        assert rs.rows == [{"?column?": 1}]
        assert rs.rowcount == 1


async def test_handle_one_and_scalar():
    def responder(spec):
        return ResultSet(rows=[{"id": 42, "total": "9.90"}], rowcount=1)

    settings, driver = _make(responder)
    async with await DataFoundation.from_settings(settings, driver=driver) as db:
        h = db.datasource("orders")
        assert (await h.one(Query("SELECT ..."))) == {"id": 42, "total": "9.90"}
        assert (await h.scalar(Query("SELECT id ..."))) == 42


async def test_execute_returns_rowcount():
    def responder(spec):
        return ResultSet(rows=[], rowcount=3)

    settings, driver = _make(responder)
    async with await DataFoundation.from_settings(settings, driver=driver) as db:
        affected = await db.execute("orders", Command("UPDATE t SET x=1"))
        assert affected == 3


async def test_pools_opened_on_start_and_closed_on_exit():
    settings, driver = _make()
    db = await DataFoundation.from_settings(settings, driver=driver)
    pool = driver.pools["postgresql://x/orders"]
    assert pool.opened is True
    await db.aclose()
    assert pool.closed is True


async def test_unknown_datasource_raises():
    settings, driver = _make()
    async with await DataFoundation.from_settings(settings, driver=driver) as db:
        with pytest.raises(ConfigError, match="unknown data source"):
            db.datasource("nope")


async def test_health_reports_per_datasource():
    settings, driver = _make(names=("orders", "analytics"))
    async with await DataFoundation.from_settings(settings, driver=driver) as db:
        health = await db.health()
        assert health == {"orders": HealthStatus.HEALTHY, "analytics": HealthStatus.HEALTHY}
        assert set(db.names()) == {"orders", "analytics"}
