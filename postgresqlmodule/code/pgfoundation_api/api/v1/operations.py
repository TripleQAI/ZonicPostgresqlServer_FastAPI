"""v1 — THE OPERATIONS TABLE (ADR-003).

**Pure data.** One row per capability; no logic lives here. Each row produces:

* a unary REST endpoint,
* an OpenAPI operation with its request schema,
* a batch-dispatch entry (P2),
* a ``/stream`` endpoint when ``streamable`` (P3).

Adding a capability is adding a row. The registry invariant tests enforce that
claim rather than trusting it.

This table is also the readable inventory of the entire v1 data plane.
"""

from __future__ import annotations

from ...registry.spec import OperationSpec
from .mappers import (
    to_command,
    to_function_query,
    to_procedure_call,
    to_query,
    to_view_query,
)
from .schemas.bulk import QueryIn
from .schemas.invocation import FunctionIn, ProcedureIn, ViewIn
from .schemas.responses import ExecuteOut, QueryOut

OPERATIONS: tuple[OperationSpec, ...] = (
    # --- governed, name-based invocation: the recommended surface ------------
    OperationSpec(
        op="view",
        suffix="view",
        request_model=ViewIn,
        response_model=QueryOut,
        to_core=to_view_query,
        to_response=QueryOut.from_result,
        handle_method="view",
        action="read",
        streamable=True,
        summary="Query a view or table by name with structured filters",
    ),
    OperationSpec(
        op="function",
        suffix="function",
        request_model=FunctionIn,
        response_model=QueryOut,
        to_core=to_function_query,
        to_response=QueryOut.from_result,
        handle_method="function",
        action="read",
        streamable=True,
        summary="Call a function by name",
    ),
    OperationSpec(
        op="procedure",
        suffix="procedure",
        request_model=ProcedureIn,
        response_model=QueryOut,
        to_core=to_procedure_call,
        to_response=QueryOut.from_result,
        handle_method="procedure",
        action="write",
        summary="Call a stored procedure by name",
    ),
    # --- advanced / trusted: raw parameterized SQL ---------------------------
    # Registered only when api.enable_raw_sql is on. Config refuses that with
    # auth disabled outside dev (doc 05 §5.5, ADR-008).
    OperationSpec(
        op="query",
        suffix="query",
        request_model=QueryIn,
        response_model=QueryOut,
        to_core=to_query,
        to_response=QueryOut.from_result,
        handle_method="query",
        action="read",
        streamable=True,
        trusted_only=True,
        summary="Run a read query (raw SQL — advanced)",
        tag="advanced",
    ),
    OperationSpec(
        op="execute",
        suffix="execute",
        request_model=QueryIn,
        response_model=ExecuteOut,
        to_core=to_command,
        to_response=ExecuteOut.from_rowcount,
        handle_method="execute",
        action="write",
        trusted_only=True,
        summary="Run a write command (raw SQL — advanced)",
        tag="advanced",
    ),
    # P3 adds: interval, fetch-since, execute-many, copy — four more rows.
)

#: ``op`` -> spec. The batch dispatcher (P2) resolves items through this, so it
#: cannot drift from the route table.
OPERATION_BY_ID: dict[str, OperationSpec] = {spec.op: spec for spec in OPERATIONS}

__all__ = ["OPERATION_BY_ID", "OPERATIONS"]
