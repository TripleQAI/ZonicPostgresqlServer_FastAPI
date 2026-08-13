"""Shared v1 field types (doc 09 §9.1b — the wire contract, frozen once shipped)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pgfoundation import FilterOp


class FilterIn(BaseModel):
    """One ``column <op> value`` predicate. Values are always bound, never inlined."""

    column: str
    op: FilterOp = FilterOp.EQ
    value: Any = None


class OrderByIn(BaseModel):
    column: str
    descending: bool = False


class SchemaQualified(BaseModel):
    """Base for the name-based payloads.

    The request field is ``schema``; internally it is ``db_schema`` because
    ``schema`` shadows a Pydantic member. The alias is part of the wire contract
    (doc 08 §8.2) — a parity test pins it.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    db_schema: str | None = Field(default=None, alias="schema")


__all__ = ["FilterIn", "OrderByIn", "SchemaQualified"]
