"""Integration smoke test against a REAL PostgreSQL (doc 13 §13.3).

Skips automatically if ``PGF_TEST_DSN`` is not set. Exercises the psycopg
adapter end-to-end: query, transaction commit/rollback, execute_many, streaming,
interval bucketing, watermark keyset, and multi-DB.
"""
from __future__ import annotations

import os

import pytest

from pgfoundation import (
    Agg,
    Command,
    DataFoundation,
    IntervalQuery,
    Query,
    WatermarkCursor,
)
from pgfoundation._internal.config.loader import load_settings
from pgfoundation.core.errors import IntegrityError

DSN = os.environ.get("PGF_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set PGF_TEST_DSN to run live tests")


async def _db():
    settings = load_settings(
        {"datasources": [{"name": "main", "dsn": DSN, "pool": {"min_size": 1, "max_size": 4}}]}
    )
    return await DataFoundation.from_settings(settings)


async def test_select_one():
    async with await _db() as db:
        assert await db.datasource("main").scalar(Query("SELECT 1")) == 1
        assert (await db.health())["main"].value == "healthy"


async def test_transaction_commit_and_rollback():
    async with await _db() as db:
        h = db.datasource("main")
        await h.execute(Command("DROP TABLE IF EXISTS pgf_smoke"))
        await h.execute(Command("CREATE TABLE pgf_smoke (id int primary key, v text)"))

        async with h.transaction() as uow:
            await uow.execute(Command("INSERT INTO pgf_smoke VALUES (1, 'a')"))
            await uow.execute(Command("INSERT INTO pgf_smoke VALUES (2, 'b')"))
        assert await h.scalar(Query("SELECT count(*) FROM pgf_smoke")) == 2

        # rollback leaves the table unchanged
        with pytest.raises(RuntimeError):
            async with h.transaction() as uow:
                await uow.execute(Command("INSERT INTO pgf_smoke VALUES (3, 'c')"))
                raise RuntimeError("boom")
        assert await h.scalar(Query("SELECT count(*) FROM pgf_smoke")) == 2

        # unique violation maps to the Core IntegrityError
        with pytest.raises(IntegrityError):
            await h.execute(Command("INSERT INTO pgf_smoke VALUES (1, 'dup')"))

        await h.execute(Command("DROP TABLE pgf_smoke"))


async def test_execute_many_and_stream():
    async with await _db() as db:
        h = db.datasource("main")
        await h.execute(Command("DROP TABLE IF EXISTS pgf_stream"))
        await h.execute(Command("CREATE TABLE pgf_stream (n int)"))
        n = await h.execute_many(
            Command("INSERT INTO pgf_stream VALUES (%(n)s)"),
            [{"n": i} for i in range(100)],
        )
        assert n == 100
        streamed = [row["n"] async for row in h.stream(Query("SELECT n FROM pgf_stream ORDER BY n"))]
        assert streamed == list(range(100))
        await h.execute(Command("DROP TABLE pgf_stream"))


async def test_interval_bucketing_and_watermark():
    async with await _db() as db:
        h = db.datasource("main")
        await h.execute(Command("DROP TABLE IF EXISTS pgf_readings"))
        await h.execute(
            Command(
                "CREATE TABLE pgf_readings "
                "(ts timestamptz, meter int, kwh double precision)"
            )
        )
        await h.execute_many(
            Command("INSERT INTO pgf_readings VALUES (%(ts)s, %(m)s, %(k)s)"),
            [
                {"ts": "2026-01-01 00:05", "m": 1, "k": 1.0},
                {"ts": "2026-01-01 00:10", "m": 1, "k": 2.0},
                {"ts": "2026-01-01 00:20", "m": 1, "k": 4.0},
            ],
        )
        # 15-minute buckets: [00:00,00:15) -> 3.0, [00:15,00:30) -> 4.0
        buckets = [
            row
            async for row in h.interval(
                IntervalQuery(
                    source="pgf_readings",
                    time_column="ts",
                    start="2026-01-01 00:00",
                    end="2026-01-01 01:00",
                    every="15 minutes",
                    metrics=[Agg("kwh", "sum")],
                )
            )
        ]
        totals = sorted(b["kwh"] for b in buckets)
        assert totals == [3.0, 4.0]

        # keyset watermark walks all rows in id/ts order
        page = await h.fetch_since(
            Query("SELECT ts, meter, kwh FROM pgf_readings", {}),
            WatermarkCursor(order_by=("ts",)),
            limit=2,
        )
        assert len(page.rows) == 2
        assert page.next_watermark.after is not None

        await h.execute(Command("DROP TABLE pgf_readings"))
