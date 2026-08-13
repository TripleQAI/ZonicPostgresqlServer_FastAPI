"""Core error hierarchy — driver-agnostic, what consumers catch.

The psycopg adapter maps ``SQLSTATE``-coded errors onto these types so consumers
never import or catch ``psycopg`` exceptions (doc 07 §7.9).
"""
from __future__ import annotations


class PgFoundationError(Exception):
    """Base class for every error the foundation raises."""


class ConfigError(PgFoundationError):
    """Invalid / missing configuration — raised at bootstrap, fail-fast."""


class ConnectionError(PgFoundationError):
    """Could not connect, or a live connection dropped."""


class PoolTimeoutError(ConnectionError):
    """Pool saturated — no connection available within the acquire timeout."""


class QueryError(PgFoundationError):
    """Bad SQL / undefined object / non-retryable execution error."""


class IntegrityError(PgFoundationError):
    """Constraint violation (unique, FK, check). Not retryable."""


class TransactionError(PgFoundationError):
    """A transaction could not begin/commit/rollback correctly."""


class TransientError(PgFoundationError):
    """Retry-eligible: serialization failure (40001), deadlock (40P01),

    transient connection loss (08xxx). See the retryable transaction runner
    (doc 07 §7.10) and the retry decorator (doc 10 §10.3).
    """


class PermanentError(PgFoundationError):
    """Explicitly non-retryable."""


__all__ = [
    "PgFoundationError",
    "ConfigError",
    "ConnectionError",
    "PoolTimeoutError",
    "QueryError",
    "IntegrityError",
    "TransactionError",
    "TransientError",
    "PermanentError",
]
