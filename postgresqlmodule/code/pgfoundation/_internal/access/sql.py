"""SQL-building safety helpers.

Values are *always* bound parameters; identifiers that must be dynamic come from
trusted config/registry and are validated here (doc 07 §7.2 / doc 16 §16.4).
"""
from __future__ import annotations

import re

from ...core.errors import QueryError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_AGG_FUNCS = {"sum", "avg", "min", "max", "count"}


def safe_identifier(name: str) -> str:
    """Allow only ``schema.table`` / ``column`` shaped identifiers."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise QueryError(f"unsafe SQL identifier: {name!r}")
    return name


def safe_agg_func(func: str) -> str:
    f = func.lower()
    if f not in _AGG_FUNCS:
        raise QueryError(f"unsupported aggregate function: {func!r}")
    return f


__all__ = ["safe_identifier", "safe_agg_func"]
