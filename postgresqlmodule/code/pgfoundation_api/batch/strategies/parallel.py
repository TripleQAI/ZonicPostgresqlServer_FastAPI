"""L4 — ParallelStrategy: bounded scatter-gather (ADR-004, doc 06 §6.3).

The default. Maximum throughput, no atomicity.

Two ways this goes wrong, both guarded here:

* an **unbounded** ``asyncio.gather`` is a denial-of-service against your own
  database — every fan-out is semaphore-bounded, and ``batch.max_items`` bounds
  the list before we reach this point;
* a gather **without** ``return_exceptions=True`` cancels every sibling when one
  item fails, turning a partial failure into a total one.
"""

from __future__ import annotations

import asyncio

from ...api.v1.schemas.batch import BatchItem, BatchItemError, BatchItemResult
from ..dispatch import dispatch
from .base import BatchContext, ExecutionStrategy


class ParallelStrategy(ExecutionStrategy):
    mode = "parallel"

    async def execute(self, items: list[BatchItem], ctx: BatchContext) -> list[BatchItemResult]:
        semaphore = asyncio.Semaphore(ctx.concurrency)
        # OBSERVE (METRIC): pgf_batch_concurrency_peak{mode} — the signal that
        #                   PROVES the fan-out is real. A peak far below the cap
        #                   means something upstream serializes (doc 11 §11.3).

        async def run(item: BatchItem) -> BatchItemResult:
            # Batch-level bound, then the per-datasource bulkhead. Items on
            # different data sources contend on different pools, so a slow
            # analytics query cannot stall the OLTP items beside it.
            async with semaphore, ctx.limiter.slot(item.datasource):
                try:
                    async with asyncio.timeout(ctx.item_timeout_s):
                        return await dispatch(item, ctx.foundation, ctx.operations)
                except TimeoutError:
                    # OBSERVE (LOG): batch.item_timeout {item_id, op, budget_ms}
                    return BatchItemResult(
                        id=item.id,
                        status="error",
                        error=BatchItemError(
                            type="TimeoutError",
                            message=f"item exceeded {ctx.item_timeout_s * 1000:.0f}ms",
                        ),
                    )

        results = await asyncio.gather(*(run(item) for item in items), return_exceptions=True)
        return [_settle(item, r) for item, r in zip(items, results, strict=True)]


def _settle(item: BatchItem, result: object) -> BatchItemResult:
    """Admission rejection and cancellation surface here as exceptions."""
    if isinstance(result, BatchItemResult):
        return result
    exc = result if isinstance(result, BaseException) else RuntimeError(str(result))
    return BatchItemResult(
        id=item.id,
        status="error",
        error=BatchItemError(type=type(exc).__name__, message=str(exc)),
    )


__all__ = ["ParallelStrategy"]
