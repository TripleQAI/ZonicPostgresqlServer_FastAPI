"""v1 — batch payloads (ADR-004, doc 06 §6.2).

An item is a **Command**: self-describing data, dispatched through the same
operation registry that generates the unary routes. There is no second switch
over operation kinds to keep in sync.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BatchMode = Literal["parallel", "sequential", "transactional"]


class BatchItem(BaseModel):
    """One operation in a batch."""

    id: str = Field(..., description="Client correlation id; echoed on the result.")
    datasource: str
    op: str = Field(..., description="Operation id from the registry, e.g. 'view'.")
    payload: dict[str, Any] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    mode: BatchMode | None = None  # falls back to batch.default_mode
    max_concurrency: int | None = None  # clamped by batch.max_concurrency
    fail_fast: bool | None = None
    timeout_ms: int | None = None  # clamped by batch.total_timeout_ms
    items: list[BatchItem] = Field(default_factory=list)


class BatchItemError(BaseModel):
    type: str
    message: str


class BatchItemResult(BaseModel):
    id: str
    status: Literal["ok", "error"]
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    elapsed_ms: float | None = None
    error: BatchItemError | None = None


class BatchResult(BaseModel):
    mode: BatchMode
    elapsed_ms: float
    ok_count: int
    error_count: int
    results: list[BatchItemResult]


__all__ = [
    "BatchItem",
    "BatchItemError",
    "BatchItemResult",
    "BatchMode",
    "BatchRequest",
    "BatchResult",
]
