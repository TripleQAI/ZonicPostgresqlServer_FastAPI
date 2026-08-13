"""P1.19 — OpenAPI snapshot.

The spec is generated, so it cannot drift from the DTOs — but it CAN drift from
what clients were promised. This pins the shape: a deliberate change updates the
snapshot in the same commit; an accidental one fails CI (doc 08 §8.5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

SNAPSHOT = Path(__file__).parent / "openapi.v1.snapshot.json"


def _summary(app) -> dict:
    """A stable projection: paths, methods, request/response schema names."""
    doc = app.openapi()
    out: dict[str, dict] = {}
    for path, operations in sorted(doc["paths"].items()):
        for method, operation in sorted(operations.items()):
            body = operation.get("requestBody")
            req = (
                body["content"]["application/json"]["schema"].get("$ref", "").rsplit("/", 1)[-1]
                if body
                else None
            )
            ok = operation["responses"].get("200", {}).get("content", {})
            res = (
                ok.get("application/json", {}).get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
                or None
            )
            out[f"{method.upper()} {path}"] = {"request": req, "response": res}
    return out


@pytest.fixture
def app():
    return create_app(ServiceSettings(profile="dev", api={"enable_raw_sql": True}))


def test_openapi_matches_snapshot(app):
    current = _summary(app)
    if not SNAPSHOT.exists():  # pragma: no cover — first run writes the baseline
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip("snapshot created; commit it")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == expected, (
        "OpenAPI changed. If intentional, update tests/contract/openapi.v1.snapshot.json "
        "in the same commit so the diff is reviewable."
    )
