"""L4 — TransactionalStrategy: all-or-nothing (ADR-004, doc 06 §6.3).

One ``UnitOfWork`` for every item, wrapped in the inherited ``run_transaction``
runner — so a serialization failure (40001) or deadlock (40P01) re-runs the
**whole body** with backoff and jitter, which is the correct granularity
(inherited ADR-010).

This delivers the ``POST .../transaction`` endpoint the Django docs specified but
never built.

**Idempotency requirement.** The body may run more than once, so items must be
free of non-DB side effects. Stated at the endpoint too, not only here.
"""

from __future__ import annotations

import time
from typing import Any

from pgfoundation import RetryPolicy

from ...api.v1.schemas.batch import BatchItem, BatchItemError, BatchItemResult
from ..dispatch import resolve
from .base import BatchContext, ExecutionStrategy


class TransactionalStrategy(ExecutionStrategy):
    mode = "transactional"

    async def execute(self, items: list[BatchItem], ctx: BatchContext) -> list[BatchItemResult]:
        if not items:
            return []

        # A transaction pins ONE connection, so every item must target the same
        # data source. Silently splitting across sources would quietly break
        # atomicity — the only thing this mode exists to provide.
        sources = {item.datasource for item in items}
        if len(sources) > 1:
            message = (
                f"transactional batches must target a single data source; got "
                f"{sorted(sources)} — atomicity cannot span connections"
            )
            return _all_failed(items, "QueryError", message)

        datasource = sources.pop()
        handle = ctx.foundation.datasource(datasource)
        collected: list[BatchItemResult] = []

        async def run(uow: Any) -> None:
            # OBSERVE (TRACE): span "pgf.transaction" wrapping every item; the whole
            #                  subtree repeats on retry — mark attempts (doc 11 §11.4)
            # Cleared per attempt: the runner may execute this body more than once,
            # and stale results from a rolled-back attempt must not survive.
            collected.clear()
            for item in items:
                spec = resolve(item, ctx.operations)
                payload = spec.request_model.model_validate(item.payload)
                started = time.perf_counter()
                result = await uow.execute(handle.compile_spec(spec.to_core(payload)))
                body = spec.to_response(result).model_dump(mode="json")
                collected.append(
                    BatchItemResult(
                        id=item.id,
                        status="ok",
                        rows=body.get("rows"),
                        row_count=body.get("row_count"),
                        elapsed_ms=body.get("elapsed_ms", (time.perf_counter() - started) * 1000.0),
                    )
                )

        async with ctx.limiter.slot(datasource):
            try:
                await handle.run_transaction(run, retry=RetryPolicy())
            except Exception as exc:  # noqa: BLE001 — translated into the envelope
                # OBSERVE (LOG): batch.rolled_back {items, failed_item_id, reason}
                # All-or-nothing: on failure NO item is reported ok, because none
                # of them committed.
                return _all_failed(items, type(exc).__name__, str(exc))

        return collected


def _all_failed(items: list[BatchItem], error_type: str, message: str) -> list[BatchItemResult]:
    return [
        BatchItemResult(
            id=item.id,
            status="error",
            error=BatchItemError(type=error_type, message=message),
        )
        for item in items
    ]


__all__ = ["TransactionalStrategy"]
