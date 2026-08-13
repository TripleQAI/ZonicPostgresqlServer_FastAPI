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
    invoke_copy,
    invoke_execute_many,
    invoke_fetch_since,
    to_command,
    to_function_query,
    to_interval_query,
    to_procedure_call,
    to_query,
    to_view_query,
)
from .schemas.bulk import CopyIn, ExecuteManyIn, QueryIn
from .schemas.interval import FetchSinceIn, IntervalIn, PageOut
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
    # --- P3: designed in the Django docs, never built there. Four rows. -----
    OperationSpec(
        op="interval",
        suffix="interval",
        request_model=IntervalIn,
        response_model=QueryOut,
        to_core=to_interval_query,
        to_response=QueryOut.from_result,
        handle_method="interval_page",
        action="read",
        streamable=True,
        summary="Time-bucketed fetch over a half-open [start, end) range",
    ),
    OperationSpec(
        op="fetch_since",
        suffix="fetch-since",
        request_model=FetchSinceIn,
        response_model=PageOut,
        to_core=lambda dto: dto,  # invoke() takes the DTO directly
        to_response=PageOut.from_page,
        handle_method="fetch_since",
        invoke=invoke_fetch_since,
        action="read",
        summary="Resumable keyset page; returns rows plus the next watermark",
    ),
    OperationSpec(
        op="execute_many",
        suffix="execute-many",
        request_model=ExecuteManyIn,
        response_model=ExecuteOut,
        to_core=lambda dto: dto,
        to_response=ExecuteOut.from_rowcount,
        handle_method="execute_many",
        invoke=invoke_execute_many,
        action="write",
        summary="Batched parameterized writes",
    ),
    OperationSpec(
        op="copy",
        suffix="copy",
        request_model=CopyIn,
        response_model=ExecuteOut,
        to_core=lambda dto: dto,
        to_response=ExecuteOut.from_rowcount,
        handle_method="copy_stream",
        invoke=invoke_copy,
        action="write",
        summary="Streaming COPY ingest",
    ),
)

#: ``op`` -> spec. The batch dispatcher (P2) resolves items through this, so it
#: cannot drift from the route table.
OPERATION_BY_ID: dict[str, OperationSpec] = {spec.op: spec for spec in OPERATIONS}

__all__ = ["OPERATION_BY_ID", "OPERATIONS"]
