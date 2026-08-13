"""v1 response envelopes.

The field names ``rows`` / ``row_count`` / ``elapsed_ms`` are a frozen contract
carried over from the Django surface (doc 08 §8.2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class QueryOut(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    elapsed_ms: float

    @classmethod
    def from_result(cls, rs: Any) -> QueryOut:
        return cls(rows=rs.rows, row_count=rs.rowcount, elapsed_ms=rs.elapsed_ms)


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
