"""v1 — interval (time-bucketed) and incremental fetch payloads.

Designed in the Django docs, never built there. Both are delivered here as
registry rows once the machinery exists (doc 01 §1.3).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AggIn(BaseModel):
    """A measure + aggregation. ``func`` is whitelisted by the core compiler."""

    column: str
    func: str = "sum"
    alias: str | None = None


class IntervalIn(BaseModel):
    """Time-bucketed read over a half-open ``[start, end)`` range."""

    source: str = Field(..., description="Governed table or view name.")
    time_column: str
    start: Any
    end: Any
    every: str = Field(..., description="Bucket width, e.g. '15 minutes'.")
    metrics: list[AggIn] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    tz: str | None = None


class WatermarkIn(BaseModel):
    order_by: list[str] = Field(default_factory=lambda: ["ts", "id"])
    after: list[Any] | None = None


class FetchSinceIn(BaseModel):
    """Resumable keyset page. Never ``OFFSET`` — the cursor is the resume point."""

    sql: str = Field(..., description="Base query; wrapped as a subquery.")
    params: dict[str, Any] = Field(default_factory=dict)
    cursor: WatermarkIn = Field(default_factory=WatermarkIn)
    limit: int = Field(1000, gt=0)


class WatermarkOut(BaseModel):
    order_by: list[str]
    after: list[Any] | None = None


class PageOut(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    next_watermark: WatermarkOut | None = None

    @classmethod
    def from_page(cls, page: Any) -> PageOut:
        wm = page.next_watermark
        return cls(
            rows=page.rows,
            row_count=len(page.rows),
            next_watermark=(
                WatermarkOut(
                    order_by=list(wm.order_by),
                    after=list(wm.after) if wm.after is not None else None,
                )
                if wm is not None
                else None
            ),
        )


__all__ = ["AggIn", "FetchSinceIn", "IntervalIn", "PageOut", "WatermarkIn", "WatermarkOut"]
