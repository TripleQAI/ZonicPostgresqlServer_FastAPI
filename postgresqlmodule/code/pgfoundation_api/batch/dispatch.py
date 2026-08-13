"""L4 — registry-driven item dispatch (ADR-003, ADR-004).

The dispatcher **receives** the operations table; it never imports one. That is
what keeps this package version-agnostic — a v2 with a different table reuses it
untouched (doc 09 §9.1b) — and it is enforced by the shell-layers contract.
"""

from __future__ import annotations

import time
from typing import Any

from pgfoundation import DataFoundation, QueryError

from ..api.v1.schemas.batch import BatchItem, BatchItemError, BatchItemResult
from ..registry.spec import OperationSpec


def resolve(item: BatchItem, operations: dict[str, OperationSpec]) -> OperationSpec:
    spec = operations.get(item.op)
    if spec is None:
        raise QueryError(f"unknown operation {item.op!r}; known: {sorted(operations)}")
    if spec.streamable and spec.op in _STREAM_ONLY:
        raise QueryError(f"operation {item.op!r} cannot be batched; use its /stream route")
    return spec


#: Streaming pins a connection for the client's read duration, so N streams in one
#: batch would hold N connections hostage to the slowest reader (doc 06 §6.5).
#: Populated in P3 when the stream routes exist.
_STREAM_ONLY: frozenset[str] = frozenset()


async def dispatch(
    item: BatchItem,
    foundation: DataFoundation,
    operations: dict[str, OperationSpec],
) -> BatchItemResult:
    """Run one item and wrap the outcome in a per-item envelope.

    Never raises for an item-level failure — the envelope carries the error so a
    sibling cannot be cancelled by it (ADR-004).
    """
    started = time.perf_counter()
    # OBSERVE (TRACE): child span "pgf.batch.item" {item_id, op, datasource}
    #                  — siblings must OVERLAP in parallel mode (doc 11 §11.4)
    try:
        spec = resolve(item, operations)
        payload = spec.request_model.model_validate(item.payload)
        handle = foundation.datasource(item.datasource)
        result = await getattr(handle, spec.handle_method)(spec.to_core(payload))
        return _ok(item, spec, result, started)
    except Exception as exc:  # noqa: BLE001 — deliberately translated per item
        # OBSERVE (METRIC): pgf_batch_items_total{mode, status="error"}
        return BatchItemResult(
            id=item.id,
            status="error",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=BatchItemError(type=type(exc).__name__, message=str(exc)),
        )


def _ok(item: BatchItem, spec: OperationSpec, result: Any, started: float) -> BatchItemResult:
    # OBSERVE (METRIC): pgf_batch_item_duration_seconds{mode, op}
    # OBSERVE (METRIC): pgf_batch_items_total{mode, status="ok"}
    body = spec.to_response(result).model_dump(mode="json")
    return BatchItemResult(
        id=item.id,
        status="ok",
        rows=body.get("rows"),
        row_count=body.get("row_count"),
        elapsed_ms=body.get("elapsed_ms", (time.perf_counter() - started) * 1000.0),
    )


__all__ = ["dispatch", "resolve"]
