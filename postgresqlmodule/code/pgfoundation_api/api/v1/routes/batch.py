"""v1 — ``POST /v1/batch`` (ADR-004, doc 06 §6.2).

One endpoint, three execution semantics selected by ``mode``. Hand-written
because a batch is not a per-datasource invocation — but it dispatches every item
through the **same operations table** that generates the unary routes, so the two
surfaces cannot drift.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from pgfoundation import DataFoundation, QueryError

from ....batch.limits import AdmissionLimiter
from ....batch.strategies.base import BatchContext, ExecutionStrategy, envelope
from ....batch.strategies.parallel import ParallelStrategy
from ....batch.strategies.sequential import SequentialPipelineStrategy
from ....batch.strategies.transactional import TransactionalStrategy
from ....bootstrap.deps import get_foundation, get_settings
from ....security.gate import Principal, authorize_global
from ....settings import ServiceSettings
from ..operations import OPERATION_BY_ID
from ..schemas.batch import BatchRequest, BatchResult

router = APIRouter(tags=["batch"])

STRATEGIES: dict[str, type[ExecutionStrategy]] = {
    "parallel": ParallelStrategy,
    "sequential": SequentialPipelineStrategy,
    "transactional": TransactionalStrategy,
}


def get_limiter(request: Request) -> AdmissionLimiter:
    return request.app.state.limiter


@router.post(
    "/batch",
    response_model=BatchResult,
    summary="Run N operations in one request",
    description=(
        "Modes: `parallel` (bounded scatter-gather across N pools), `sequential` "
        "(one pinned connection, pipelined), `transactional` (one UnitOfWork, "
        "all-or-nothing, retried whole on 40001/40P01).\n\n"
        "**Partial success is normal**: HTTP stays 200 and each item carries its "
        "own `status`. Only whole-batch failures (validation, admission, total "
        "timeout) produce a non-200.\n\n"
        "**Transactional bodies may run more than once** — the retry runner "
        "re-executes the whole batch on a serialization failure, so items must be "
        "free of non-database side effects."
    ),
)
async def run_batch(
    body: BatchRequest,
    foundation: Annotated[DataFoundation, Depends(get_foundation)],
    settings: Annotated[ServiceSettings, Depends(get_settings)],
    limiter: Annotated[AdmissionLimiter, Depends(get_limiter)],
    principal: Annotated[Principal, Depends(authorize_global("write", "batch"))],  # noqa: ARG001
) -> BatchResult:
    cfg = settings.batch
    if not cfg.enabled:
        raise QueryError("batch is disabled by configuration")

    # Bound the list BEFORE any database work — a rejection must be cheap.
    if len(body.items) > cfg.max_items:
        raise QueryError(
            f"batch has {len(body.items)} items; the configured maximum is {cfg.max_items}"
        )

    ids = [item.id for item in body.items]
    if len(set(ids)) != len(ids):
        raise QueryError("batch item ids must be unique — results correlate by id")

    mode = body.mode or cfg.default_mode
    # Clamp, never trust: a client cannot raise its own concurrency ceiling.
    concurrency = min(body.max_concurrency or cfg.max_concurrency, cfg.max_concurrency)
    total_timeout = min(body.timeout_ms or cfg.total_timeout_ms, cfg.total_timeout_ms)

    ctx = BatchContext(
        foundation=foundation,
        operations=OPERATION_BY_ID,
        limiter=limiter,
        concurrency=concurrency,
        item_timeout_s=cfg.item_timeout_ms / 1000.0,
        fail_fast=cfg.fail_fast if body.fail_fast is None else body.fail_fast,
    )

    started = time.perf_counter()
    # OBSERVE (METRIC): pgf_batch_requests_total{mode}, pgf_batch_items{mode}
    # OBSERVE (TRACE):  span "pgf.batch" {mode, items}
    strategy = STRATEGIES[mode]()
    try:
        async with asyncio.timeout(total_timeout / 1000.0):
            results = await strategy.execute(body.items, ctx)
    except TimeoutError:
        raise QueryError(f"batch exceeded its {total_timeout}ms budget") from None

    return envelope(mode, results, started)


__all__ = ["router"]
