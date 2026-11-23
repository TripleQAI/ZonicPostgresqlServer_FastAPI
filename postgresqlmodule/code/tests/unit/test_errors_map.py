"""SQLSTATE -> Core error mapping (doc 07 §7.9), with psycopg's real classes."""
from __future__ import annotations

import psycopg.errors as pe
import pytest

from pgfoundation import ConnectionError as PgConnectionError
from pgfoundation import IntegrityError, QueryError, TransientError
from pgfoundation._internal.drivers.errors_map import map_error


@pytest.mark.parametrize("sqlstate", ["57P01", "57P02", "57P03"])
def test_server_ending_the_session_is_a_connection_error(sqlstate):
    """57P01 is what Neon sends when a compute scales to zero under an open
    connection. It surfaced as a 400 QueryError -- blaming the caller -- because
    class 57 had no case and psycopg names it AdminShutdown, not *OperationalError."""
    exc = pe.lookup(sqlstate)("terminating connection due to administrator command")
    assert isinstance(map_error(exc), PgConnectionError)


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("08006", PgConnectionError),  # connection_failure
        ("40001", TransientError),     # serialization_failure
        ("57014", TransientError),     # query_canceled (statement_timeout)
        ("23505", IntegrityError),     # unique_violation
        ("42P01", QueryError),         # undefined_table
    ],
)
def test_existing_mappings_are_unchanged(sqlstate, expected):
    assert isinstance(map_error(pe.lookup(sqlstate)("x")), expected)
