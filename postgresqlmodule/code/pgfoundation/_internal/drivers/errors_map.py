"""Map psycopg ``SQLSTATE`` errors onto the Core hierarchy (doc 07 §7.9)."""
from __future__ import annotations

from ...core import errors as e


def map_error(exc: Exception) -> e.PgFoundationError:
    """Translate a psycopg exception into a driver-agnostic Core error."""
    sqlstate = getattr(exc, "sqlstate", None)
    msg = str(exc)

    if sqlstate:
        cls = sqlstate[:2]
        if sqlstate in ("40001", "40P01"):  # serialization failure / deadlock
            return e.TransientError(msg)
        if cls == "08":  # connection exception
            return e.ConnectionError(msg)
        # admin_shutdown / crash_shutdown / cannot_connect_now: the server ended
        # or refused the session -- what Neon sends when a compute scales to
        # zero under an open connection. Class 57 is otherwise "operator
        # intervention", and psycopg names this AdminShutdown, so neither the
        # class check above nor the name fallback below caught it: it surfaced
        # as a 400 QueryError, blaming the caller for the server going away.
        if sqlstate in ("57P01", "57P02", "57P03"):
            return e.ConnectionError(msg)
        if cls == "23":  # integrity constraint violation
            return e.IntegrityError(msg)
        if cls in ("42", "22"):  # syntax/undefined object / data exception
            return e.QueryError(msg)
        if sqlstate == "57014":  # statement_timeout / query_canceled
            return e.TransientError(msg)

    # Fall back on psycopg class names when SQLSTATE is absent (e.g. pool/conn).
    name = type(exc).__name__
    if "OperationalError" in name or "InterfaceError" in name:
        return e.ConnectionError(msg)
    return e.QueryError(msg)


__all__ = ["map_error"]
