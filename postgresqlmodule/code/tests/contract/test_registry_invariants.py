"""P1.17 — the registry invariants (doc 10 §10.3, ADR-003).

These tests are what make the low-code claim falsifiable:

    Adding a capability touches exactly one data row plus its DTO.
    No route file, no OpenAPI file, no batch switch, no auth call site.

Without them the mechanism quietly decays back into hand-written endpoints.
"""

from __future__ import annotations

import pytest

from pgfoundation_api.api.v1 import PREFIX
from pgfoundation_api.api.v1.operations import OPERATION_BY_ID, OPERATIONS
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

#: Routes that legitimately are not registry-derived: the unversioned probes and
#: the hand-written v1 admin routes. Anything else on the data plane means
#: someone bypassed the table (doc 09 §9.1a).
ALLOWED_NON_REGISTRY = {
    "/health",
    "/ready",
    f"{PREFIX}/health",
    f"{PREFIX}/ready",
    f"{PREFIX}/datasources",
    # Hand-written because a batch is not a per-datasource invocation — but it
    # dispatches every item through the SAME operations table, so the two
    # surfaces still cannot drift (ADR-004).
    f"{PREFIX}/batch",
    # A GET with no body and an unbounded lifetime — not an operation in the
    # registry sense, so it is hand-written (ADR-006).
    f"{PREFIX}/datasources/{{name}}/subscribe/{{channel}}",
}


@pytest.fixture
def app_all_ops():
    """Raw SQL on, so trusted_only rows are registered too."""
    return create_app(
        ServiceSettings(profile="dev", api={"enable_raw_sql": True}, auth={"enabled": False})
    )


def paths(app) -> set[str]:
    """Every registered path.

    Deliberately read from the generated OpenAPI document rather than by walking
    ``app.routes``. FastAPI wraps ``include_router`` results in private
    ``_IncludedRouter`` nodes that expose neither ``.path`` nor ``.routes``, and
    nested includes leave prefixes unresolved on the objects they *do* expose —
    so a route walk is both version-fragile and, worse, silently returns an
    almost-empty set that makes these invariants pass vacuously.

    The OpenAPI document is the public, stable projection of the route table. It
    is also the artifact clients actually consume, so pinning invariants to it is
    the more meaningful assertion. Routes are additionally proven to *work* end
    to end by the parity harness.
    """
    return set(app.openapi()["paths"])


@pytest.mark.parametrize("spec", OPERATIONS, ids=lambda s: s.op)
def test_every_operation_has_a_route(app_all_ops, spec):
    assert f"{PREFIX}/datasources/{{name}}/{spec.suffix}" in paths(app_all_ops)


@pytest.mark.parametrize("spec", OPERATIONS, ids=lambda s: s.op)
def test_every_operation_is_batch_dispatchable(spec):
    """P2's dispatcher resolves items through this same table."""
    assert OPERATION_BY_ID[spec.op] is spec


@pytest.mark.parametrize("spec", OPERATIONS, ids=lambda s: s.op)
def test_openapi_documents_every_operation(app_all_ops, spec):
    """THE detector for the P1.9 silent-failure mode.

    If the generated signature is not introspected, the route still exists but
    its request schema vanishes — the endpoint 400s on every call and the spec
    is useless for client generation, with nothing raising.
    """
    spec_doc = app_all_ops.openapi()
    path = f"{PREFIX}/datasources/{{name}}/{spec.suffix}"
    assert path in spec_doc["paths"], f"{spec.op} missing from OpenAPI"

    operation = spec_doc["paths"][path]["post"]
    ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith(f"/{spec.request_model.__name__}"), (
        f"{spec.op} documents {ref}, expected {spec.request_model.__name__}"
    )


def test_no_body_leaks_as_a_query_parameter(app_all_ops):
    """The precise shape of the P1.9 failure: `body` degrades to a query param."""
    doc = app_all_ops.openapi()
    for path, operations in doc["paths"].items():
        for method, operation in operations.items():
            query = [p["name"] for p in operation.get("parameters", []) if p["in"] == "query"]
            assert not query, f"{method.upper()} {path} has query params: {query}"


def test_no_data_plane_route_outside_the_registry(app_all_ops):
    """The test that stops the mechanism decaying into hand-written views."""
    generated = {f"{PREFIX}/datasources/{{name}}/{s.suffix}" for s in OPERATIONS}
    generated |= {
        f"{PREFIX}/datasources/{{name}}/{s.suffix}/stream" for s in OPERATIONS if s.streamable
    }
    unexplained = paths(app_all_ops) - generated - ALLOWED_NON_REGISTRY
    assert not unexplained, f"routes bypassing the registry: {sorted(unexplained)}"


def test_trusted_ops_absent_when_raw_sql_disabled():
    """Disabled means *not registered* — absent from routes AND from the spec."""
    app = create_app(ServiceSettings(api={"enable_raw_sql": False}))
    registered = paths(app)
    doc = app.openapi()

    for spec in OPERATIONS:
        path = f"{PREFIX}/datasources/{{name}}/{spec.suffix}"
        if spec.trusted_only:
            assert path not in registered, f"{spec.op} should not be registered"
            assert path not in doc["paths"], f"{spec.op} should not be documented"
        else:
            assert path in registered


def test_operation_ids_are_unique():
    assert len(OPERATION_BY_ID) == len(OPERATIONS)


def test_every_operation_declares_an_action():
    """The factory turns this into the auth dependency, so it cannot be omitted."""
    for spec in OPERATIONS:
        assert spec.action in {"read", "write", "list"}


@pytest.mark.parametrize("spec", [s for s in OPERATIONS if s.streamable], ids=lambda s: s.op)
def test_streamable_operations_have_a_stream_route(app_all_ops, spec):
    """`streamable=True` is one flag on one row; the route follows (ADR-006)."""
    assert f"{PREFIX}/datasources/{{name}}/{spec.suffix}/stream" in paths(app_all_ops)


@pytest.mark.parametrize("spec", [s for s in OPERATIONS if not s.streamable], ids=lambda s: s.op)
def test_non_streamable_operations_have_no_stream_route(app_all_ops, spec):
    assert f"{PREFIX}/datasources/{{name}}/{spec.suffix}/stream" not in paths(app_all_ops)
