"""Retryable transaction runner — re-runs the whole body on 40001/40P01.

The key mechanism from doc 07 §7.10: a serialization failure invalidates the
*entire* transaction, so statement-level retry is insufficient — the body (a
callable) is re-executed with backoff + jitter.
"""
from __future__ import annotations

import random
from typing import Any, Awaitable, Callable

from ...core.errors import TransientError
from ...core.models import IsolationLevel, RetryPolicy
from ...core.ports import ClockPort, PoolPort
from ..clock import SystemClock
from .uow import UnitOfWork

TxnBody = Callable[[UnitOfWork], Awaitable[Any]]


async def run_transaction(
    pool: PoolPort,
    fn: TxnBody,
    *,
    isolation: IsolationLevel | None = None,
    retry: RetryPolicy | None = None,
    clock: ClockPort | None = None,
) -> Any:
    """Run ``fn(uow)`` in a transaction, retrying the whole body on TransientError.

    ``fn`` may run more than once — keep it free of non-DB side effects (doc 07 §7.10.4).
    """
    retry = retry or RetryPolicy()
    clock = clock or SystemClock()
    attempt = 0
    while True:
        attempt += 1
        try:
            async with UnitOfWork(pool, isolation) as uow:
                return await fn(uow)
        except TransientError:
            if attempt >= retry.max_attempts:
                raise
            await _backoff(clock, attempt, retry)


async def _backoff(clock: ClockPort, attempt: int, retry: RetryPolicy) -> None:
    ceiling = min(
        retry.max_backoff_ms, retry.base_backoff_ms * (2 ** (attempt - 1))
    )
    delay_ms = random.uniform(0, ceiling) if retry.jitter else ceiling
    await clock.sleep(delay_ms / 1000.0)


__all__ = ["run_transaction", "TxnBody"]
