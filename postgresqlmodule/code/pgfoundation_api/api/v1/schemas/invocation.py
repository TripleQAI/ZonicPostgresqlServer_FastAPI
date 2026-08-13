"""Governed, name-based invocation payloads — the recommended v1 surface.

Callers name a database object and pass parameters; no SQL crosses the wire.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .common import FilterIn, OrderByIn, SchemaQualified


class ViewIn(SchemaQualified):
    """Query a view/table by NAME with structured filters — no SQL."""

    columns: list[str] | None = None
    filters: list[FilterIn] = Field(default_factory=list)
    order_by: list[OrderByIn] = Field(default_factory=list)
    limit: int | None = None
    offset: int | None = None


class FunctionIn(SchemaQualified):
    """Call a function by NAME with args (named mapping or positional list)."""

    args: dict[str, Any] | list[Any] | None = None
    scalar: bool = False


class ProcedureIn(SchemaQualified):
    """Call a stored procedure by NAME via CALL (positional args)."""

    args: list[Any] | None = None


__all__ = ["FunctionIn", "ProcedureIn", "ViewIn"]
