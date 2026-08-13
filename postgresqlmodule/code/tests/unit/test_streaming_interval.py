"""Phase 1.5 — streaming, interval bucketing, watermark keyset, copy_stream."""
from __future__ import annotations

import pytest

from pgfoundation import (
    Agg,
    DataFoundation,
    IntervalQuery,
    Query,
    ResultSet,
    Row,
    WatermarkCursor,
)
from pgfoundation._internal.access.interval import compile_interval
from pgfoundation._internal.access.watermark import build_keyset_query, next_watermark
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation.core.errors import QueryError


async def _foundation(responder=None):
    settings = load_settings({"datasources": [{"name": "db", "dsn": "postgresql://x/db"}]})
    driver = FakeDriver(responder)
    db = await DataFoundation.from_settings(settings, driver=driver)
    return db, driver.pools["postgresql://x/db"]


# --- interval bucketing ---

def test_compile_interval_generates_parameterized_bucket_sql():
    q = compile_interval(
        IntervalQuery(
            source="readings",
            time_column="ts",
            start="2026-01-01",
            end="2026-02-01",
            every="15 minutes",
            metrics=[Agg("kwh", "sum"), Agg("demand_kw", "avg", alias="demand")],
            group_by=["meter_id"],
        )
    )
    assert "date_bin(%(__every)s::interval, ts, %(__start)s) AS bucket" in q.sql
    assert "sum(kwh) AS kwh" in q.sql
    assert "avg(demand_kw) AS demand" in q.sql
    assert "GROUP BY bucket, meter_id" in q.sql
    assert q.params == {"__every": "15 minutes", "__start": "2026-01-01", "__end": "2026-02-01"}


def test_interval_rejects_unsafe_identifiers():
    with pytest.raises(QueryError, match="unsafe SQL identifier"):
        compile_interval(
            IntervalQuery(source="readings; DROP TABLE x", time_column="ts",
                          start=1, end=2, every="1 hour")
        )


def test_interval_rejects_unknown_agg_func():
    with pytest.raises(QueryError, match="unsupported aggregate"):
        compile_interval(
            IntervalQuery(source="t", time_column="ts", start=1, end=2,
                          every="1 hour", metrics=[Agg("kwh", "median")])
        )


# --- watermark / keyset ---

def test_keyset_query_first_page_has_no_where():
    q = build_keyset_query(
        Query("SELECT ts, id, kwh FROM readings WHERE meter=%(m)s", {"m": 7}),
        WatermarkCursor(order_by=("ts", "id"), after=None),
        limit=100,
    )
    assert "WHERE" not in q.sql.split("__sub")[1]  # no keyset predicate on first page
    assert "ORDER BY ts, id" in q.sql
    assert "LIMIT 100" in q.sql
    assert q.params == {"m": 7}


def test_keyset_query_uses_tuple_comparison_after_watermark():
    q = build_keyset_query(
        Query("SELECT ts, id FROM readings", {}),
        WatermarkCursor(order_by=("ts", "id"), after=("2026-01-01", 42)),
        limit=50,
    )
    assert "(ts, id) > (%(__k0)s, %(__k1)s)" in q.sql
    assert q.params == {"__k0": "2026-01-01", "__k1": 42}


def test_next_watermark_advances_from_last_row():
    rows = [{"ts": "t1", "id": 1}, {"ts": "t2", "id": 2}]
    wm = next_watermark(rows, WatermarkCursor(order_by=("ts", "id")))
    assert wm.after == ("t2", 2)


def test_next_watermark_unchanged_on_empty_page():
    cur = WatermarkCursor(order_by=("ts", "id"), after=("t0", 0))
    assert next_watermark([], cur) is cur


async def test_fetch_since_returns_page_and_next_watermark():
    def responder(spec):
        return ResultSet(rows=[{"ts": "t1", "id": 1}, {"ts": "t2", "id": 2}], rowcount=2)

    db, pool = await _foundation(responder)
    page = await db.datasource("db").fetch_since(
        Query("SELECT ts, id FROM readings WHERE meter=%(m)s", {"m": 1}),
        WatermarkCursor(order_by=("ts", "id")),
        limit=1000,
    )
    assert len(page.rows) == 2
    assert page.next_watermark.after == ("t2", 2)


async def test_fetch_since_requires_named_params():
    db, pool = await _foundation()
    with pytest.raises(QueryError, match="named .dict. params"):
        await db.datasource("db").fetch_since(
            Query("SELECT * FROM t WHERE a=%s", [1]),
            WatermarkCursor(),
            limit=10,
        )


# --- streaming + copy ---

async def test_stream_yields_rows():
    def responder(spec):
        return ResultSet(rows=[{"n": 1}, {"n": 2}, {"n": 3}], rowcount=3)

    db, pool = await _foundation(responder)
    got = [row async for row in db.datasource("db").stream(Query("SELECT n FROM t"))]
    assert got == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert pool.released == 1  # connection returned after the stream closed


async def test_copy_stream_counts_rows_and_validates_table():
    db, pool = await _foundation()
    n = await db.datasource("db").copy_stream("readings", [(1, 2), (3, 4), (5, 6)])
    assert n == 3
    assert pool.copied == [("readings", 3)]

    with pytest.raises(QueryError, match="unsafe SQL identifier"):
        await db.datasource("db").copy_stream("bad; DROP", [(1,)])
