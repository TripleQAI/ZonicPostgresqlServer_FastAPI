"""v1 response envelopes.

The field names ``rows`` / ``row_count`` / ``elapsed_ms`` are a frozen contract
carried over from the Django surface (doc 08 §8.2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class QueryOut(BaseModel):
    """Read envelope (ADR-007).

    In ``dict`` row format ``rows`` holds mappings and ``columns`` is absent, so
    existing clients see exactly what they saw before. In ``tuple`` format
    ``rows`` holds arrays and ``columns`` names them.
    """

    # The union is at the ITEM level, not the list level: a list-of-unions
    # validates each row against both shapes, whereas a union-of-lists reports
    # the first member's failure and never falls through to the second.
    rows: list[dict[str, Any] | list[Any]]
    row_count: int
    elapsed_ms: float
    columns: list[str] | None = None

    @classmethod
    def from_result(cls, rs: Any) -> QueryOut:
        return cls(
            rows=rs.rows,
            row_count=rs.rowcount,
            elapsed_ms=rs.elapsed_ms,
            columns=getattr(rs, "columns", None),
        )


class ExecuteOut(BaseModel):
    row_count: int
    elapsed_ms: float = 0.0

    @classmethod
    def from_rowcount(cls, n: int) -> ExecuteOut:
        return cls(row_count=n)


class DataSourceList(BaseModel):
    datasources: list[str]


class ErrorOut(BaseModel):
    error: str
    type: str
    request_id: str | None = None


__all__ = ["DataSourceList", "ErrorOut", "ExecuteOut", "QueryOut"]
