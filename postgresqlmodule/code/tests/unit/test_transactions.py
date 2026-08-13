"""Phase 1 — Unit of Work, retryable transaction, execute_many, Repository."""
from __future__ import annotations

import pytest

from pgfoundation import (
    Command,
    DataFoundation,
    IsolationLevel,
    Query,
    Repository,
    ResultSet,
    RetryPolicy,
)
from pgfoundation._internal.clock import ManualClock
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation.core.errors import IntegrityError, TransientError


def _make(responder=None, clock=None):
    settings = load_settings({"datasources": [{"name": "db", "dsn": "postgresql://x/db"}]})
    driver = FakeDriver(responder)
    return settings, driver, clock


async def _foundation(responder=None, clock=None):
    settings, driver, clock = _make(responder, clock)
    db = await DataFoundation.from_settings(settings, driver=driver, clock=clock)
    return db, driver.pools["postgresql://x/db"]


async def test_uow_commits_on_clean_exit():
    db, pool = await _foundation()
    async with db.transaction("db") as uow:
        await uow.execute(Command("UPDATE a SET x=1"))
        await uow.execute(Command("UPDATE b SET y=2"))
    assert pool.begins == 1
    assert pool.commits == 1
    assert pool.rollbacks == 0
    assert pool.released == 1  # connection returned to pool


async def test_uow_rolls_back_on_exception():
    db, pool = await _foundation()
    with pytest.raises(RuntimeError):
        async with db.transaction("db") as uow:
            await uow.execute(Command("UPDATE a SET x=1"))
            raise RuntimeError("boom")
    assert pool.commits == 0
    assert pool.rollbacks == 1
    assert pool.released == 1


async def test_savepoint_nesting_does_not_double_commit():
    db, pool = await _foundation()
    async with db.transaction("db", isolation=IsolationLevel.SERIALIZABLE) as uow:
        await uow.execute(Command("UPDATE a SET x=1"))
        async with uow.savepoint() as sp:
            await sp.execute(Command("UPDATE b SET y=2"))
    assert pool.begins == 1  # one outer transaction
    assert pool.commits == 1


async def test_run_transaction_retries_then_succeeds():
    calls = {"n": 0}

    async def body(uow):
        calls["n"] += 1
        if calls["n"] < 3:  # fail transiently twice, then succeed
            raise TransientError("40001 serialization failure")
        await uow.execute(Command("UPDATE a SET x=1"))
        return "ok"

    clock = ManualClock()
    db, pool = await _foundation(clock=clock)
    result = await db.run_transaction(
        "db", body, retry=RetryPolicy(max_attempts=5, base_backoff_ms=10)
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert pool.commits == 1        # only the successful attempt commits
    assert pool.rollbacks == 2      # the two transient failures rolled back
    assert len(clock.slept) == 2    # backed off twice


async def test_run_transaction_gives_up_after_max_attempts():
    async def body(uow):
        raise TransientError("40P01 deadlock")

    clock = ManualClock()
    db, pool = await _foundation(clock=clock)
    with pytest.raises(TransientError):
        await db.run_transaction("db", body, retry=RetryPolicy(max_attempts=3))
    assert pool.rollbacks == 3


async def test_non_transient_error_is_not_retried():
    async def body(uow):
        raise IntegrityError("23505 unique violation")

    clock = ManualClock()
    db, pool = await _foundation(clock=clock)
    with pytest.raises(IntegrityError):
        await db.run_transaction("db", body, retry=RetryPolicy(max_attempts=5))
    assert pool.rollbacks == 1      # tried once, not retried
    assert clock.slept == []


async def test_execute_many_returns_affected():
    db, pool = await _foundation()
    n = await db.datasource("db").execute_many(
        Command("INSERT INTO t VALUES (%(v)s)"), [{"v": 1}, {"v": 2}, {"v": 3}]
    )
    assert n == 3


async def test_repository_maps_rows():
    def responder(spec):
        return ResultSet(rows=[{"id": 1}, {"id": 2}], rowcount=2)

    db, pool = await _foundation(responder=responder)
    repo = Repository(db.datasource("db"), mapper=lambda r: r["id"])
    assert await repo.find(Query("SELECT id FROM t")) == [1, 2]
