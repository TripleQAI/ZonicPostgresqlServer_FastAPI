"""L4 — SequentialPipelineStrategy: ordered, few round-trips (ADR-004, doc 06 §6.3).

One pinned connection for the whole batch, statements issued in psycopg
**pipeline mode**. Order is guaranteed, so later items may depend on earlier
writes.

Use when round-trips — not the database — are the bottleneck, which is the normal
case across a WAN to a managed instance (doc 12 §12.6).

Not atomic: use ``transactional`` when all-or-nothing is required.
"""

from __future__ import annotations

import time

from ...api.v1.schemas.batch import BatchItem, BatchItemError, BatchItemResult
from ..dispatch import resolve
from .base import BatchContext, ExecutionStrategy


class SequentialPipelineStrategy(ExecutionStrategy):
    mode = "sequential"

    async def execute(self, items: list[BatchItem], ctx: BatchContext) -> list[BatchItemResult]:
        if not items:
            return []

        # One pinned connection means one data source, by definition.
        sources = {item.datasource for item in items}
        if len(sources) > 1:
            message = (
                f"sequential batches must target a single data source; got "
                f"{sorted(sources)} — one pinned connection cannot span sources. "
                f"Use mode=parallel to fan out across data sources."
            )
            return [
                BatchItemResult(
                    id=item.id,
                    status="error",
                    error=BatchItemError(type="QueryError", message=message),
                )
                for item in items
            ]

        datasource = sources.pop()
        handle = ctx.foundation.datasource(datasource)

        # Compile every item up front: a bad payload should not consume a
        # connection or half-execute the batch.
        specs = []
        for item in items:
            try:
                spec = resolve(item, ctx.operations)
                payload = spec.request_model.model_validate(item.payload)
                # Compile here: a pinned connection executes QuerySpecs, and
                # governed value objects are not specs until compiled.
                specs.append((item, spec, handle.compile_spec(spec.to_core(payload))))
            except Exception as exc:  # noqa: BLE001
                return [
                    BatchItemResult(
                        id=i.id,
                        status="error",
                        error=BatchItemError(
                            type=type(exc).__name__,
                            message=(
                                str(exc)
                                if i.id == item.id
                                else f"batch aborted: item {item.id!r} failed to compile"
                            ),
                        ),
                    )
                    for i in items
                ]

        started = time.perf_counter()
        async with ctx.limiter.slot(datasource):
            try:
                # OBSERVE (METRIC): pgf_batch_item_duration_seconds{mode="sequential"}
                results = await handle.run_pipeline([core for _, _, core in specs])
            except Exception as exc:  # noqa: BLE001
                return [
                    BatchItemResult(
                        id=item.id,
                        status="error",
                        error=BatchItemError(type=type(exc).__name__, message=str(exc)),
                    )
                    for item, _, _ in specs
                ]

        elapsed = (time.perf_counter() - started) * 1000.0
        out: list[BatchItemResult] = []
        for (item, spec, _), result in zip(specs, results, strict=True):
            body = spec.to_response(result).model_dump(mode="json")
            out.append(
                BatchItemResult(
                    id=item.id,
                    status="ok",
                    rows=body.get("rows"),
                    row_count=body.get("row_count"),
                    elapsed_ms=body.get("elapsed_ms", elapsed / max(len(specs), 1)),
                )
            )
        return out


__all__ = ["SequentialPipelineStrategy"]
