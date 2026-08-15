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


def quote_identifier(name: str) -> str:
    """Validate, then render as a **quoted** identifier: ``"schema"."table"``.

    PostgreSQL folds unquoted identifiers to lower case. Emitting a validated
    name verbatim therefore makes every object whose name is not already
    lower case unreachable through the governed API — a table created as
    ``"agentBuilder_agentworkflow"`` (which is what Django and most ORMs do)
    resolves to ``agentbuilder_agentworkflow`` and fails with *relation does not
    exist*. Callers were then pushed onto the raw-SQL endpoint, which is exactly
    the surface the design keeps off production.

    Quoting is also strictly **safer** than not quoting: ``_IDENT`` already
    excludes the quote character, so the emitted identifier cannot terminate its
    own quoting, and a quoted identifier is never re-interpreted as a keyword.

    Lower-case names are unaffected — ``"id"`` and ``id`` denote the same column.
    """
    return ".".join(f'"{part}"' for part in safe_identifier(name).split("."))


def safe_agg_func(func: str) -> str:
    f = func.lower()
    if f not in _AGG_FUNCS:
        raise QueryError(f"unsupported aggregate function: {func!r}")
    return f


__all__ = ["safe_identifier", "quote_identifier", "safe_agg_func"]
