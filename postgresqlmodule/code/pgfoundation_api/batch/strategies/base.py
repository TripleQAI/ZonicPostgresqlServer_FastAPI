"""L4 — the ExecutionStrategy protocol + shared envelope assembly (ADR-004).

The batch lifecycle is a **Template Method**: validate -> admit -> dispatch ->
collect -> envelope. Only the dispatch step varies, so a fourth mode is a fourth
class rather than a fourth branch in a growing conditional.
"""

from __future__ import annotations

import time
from typing import Protocol

from pgfoundation import DataFoundation

from ...api.v1.schemas.batch import BatchItem, BatchItemResult, BatchMode, BatchResult
from ...registry.spec import OperationSpec
from ..limits import AdmissionLimiter


class BatchContext:
    """Everything a strategy needs, resolved once by the route."""

    def __init__(
        self,
        *,
        foundation: DataFoundation,
        operations: dict[str, OperationSpec],
        limiter: AdmissionLimiter,
        concurrency: int,
        item_timeout_s: float,
        fail_fast: bool,
    ) -> None:
        self.foundation = foundation
        self.operations = operations
        self.limiter = limiter
        self.concurrency = concurrency
        self.item_timeout_s = item_timeout_s
        self.fail_fast = fail_fast


class ExecutionStrategy(Protocol):
    mode: BatchMode

    async def execute(self, items: list[BatchItem], ctx: BatchContext) -> list[BatchItemResult]: ...


def envelope(mode: BatchMode, results: list[BatchItemResult], started: float) -> BatchResult:
    """Assemble the response. Results stay in **request order**, always."""
    ok = sum(1 for r in results if r.status == "ok")
    # OBSERVE (METRIC): pgf_batch_duration_seconds{mode}, pgf_batch_items{mode}
    # OBSERVE (LOG):    batch.completed {mode, items, ok, errors, elapsed_ms}
    return BatchResult(
        mode=mode,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        ok_count=ok,
        error_count=len(results) - ok,
        results=results,
    )


__all__ = ["BatchContext", "ExecutionStrategy", "envelope"]
