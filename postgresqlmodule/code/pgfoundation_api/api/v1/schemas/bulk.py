"""Raw-SQL and bulk payloads.

``QueryIn`` is the **advanced/trusted** surface: it is only registered when
``api.enable_raw_sql`` is on, and config refuses that combination without auth
outside dev (doc 05 §5.5, ADR-008).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryIn(BaseModel):
    """Advanced/trusted: raw parameterized SQL. Prefer the name-based calls."""

    sql: str = Field(..., description="Parameterized SQL; values go in `params`.")
    params: dict[str, Any] | list[Any] | None = None


class ExecuteManyIn(BaseModel):
    """Batched parameterized writes — one protocol exchange for many rows."""

    sql: str = Field(..., description="Parameterized SQL run once per parameter set.")
    params_seq: list[dict[str, Any] | list[Any]] = Field(default_factory=list)


class CopyIn(BaseModel):
    """Streaming COPY ingest. `table` is validated as an identifier by the core."""

    table: str
    rows: list[list[Any]] = Field(default_factory=list)


__all__ = ["CopyIn", "ExecuteManyIn", "QueryIn"]
