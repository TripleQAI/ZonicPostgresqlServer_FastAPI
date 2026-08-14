"""Run the full API against an in-memory fake driver — no database required.

    python run_demo.py

Serves every route, Swagger UI at /docs, and the requests in `curls/` with
canned data. Useful for exploring the surface, demoing it, or working on the
shell while the database is unavailable.

**Why this lives outside `pgfoundation_api/`.** It imports `FakeDriver` from
`pgfoundation._internal`, and the `shell-uses-public-surface-only` architecture
contract forbids the shell package from reaching into `_internal` — that rule is
what guarantees library/service parity. A top-level dev script is not part of the
shell, so the contract holds and this stays honest rather than exempted.

Not a test double for correctness: the fake driver returns canned rows and has
no SQL engine. Use the real service for anything you intend to trust.
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

_CODE_ROOT = str(Path(__file__).resolve().parent)
if _CODE_ROOT not in sys.path:
    sys.path.insert(0, _CODE_ROOT)

from pgfoundation import DataFoundation, QuerySpec, ResultSet  # noqa: E402
from pgfoundation._internal.config.loader import load_settings  # noqa: E402
from pgfoundation._internal.drivers.fake import FakeDriver  # noqa: E402
from pgfoundation_api.app import create_app  # noqa: E402
from pgfoundation_api.settings import load_service_settings  # noqa: E402

#: Mirrors the demo objects in `curls/CURLS.md`, so the collection returns
#: something recognisable rather than empty rows.
ORDERS = [
    {"id": 1, "customer": "alice", "amount": Decimal("19.90"), "status": "paid"},
    {"id": 3, "customer": "bob", "amount": Decimal("42.00"), "status": "paid"},
    {"id": 4, "customer": "bob", "amount": Decimal("7.50"), "status": "paid"},
]
READINGS = [
    {
        "bucket": datetime(2026, 8, 13, hour, 0, tzinfo=UTC),
        "meter_id": 1,
        "kwh": Decimal(f"{hour * 1.5:.2f}"),
    }
    for hour in range(6)
]


def responder(spec: QuerySpec) -> ResultSet:
    """Canned results shaped by what the SQL looks like."""
    sql = spec.sql.strip().upper()
    if sql.startswith("SET"):
        return ResultSet(rows=[], rowcount=0)
    if "DATE_BIN" in sql or "READINGS" in sql:
        return ResultSet(rows=list(READINGS), rowcount=len(READINGS), elapsed_ms=3.4)
    if sql.startswith(("SELECT", "CALL")):
        return ResultSet(rows=list(ORDERS), rowcount=len(ORDERS), elapsed_ms=1.8)
    return ResultSet(rows=[], rowcount=len(ORDERS), elapsed_ms=0.6)


async def build_foundation() -> DataFoundation:
    settings = load_settings(
        {
            "datasources": [
                {"name": "main", "dsn": "postgresql://demo@localhost/main"},
                {"name": "analytics", "dsn": "postgresql://demo@localhost/analytics"},
            ]
        }
    )
    return await DataFoundation.from_settings(settings, driver=FakeDriver(responder))


def main() -> None:
    import uvicorn

    # Raw SQL on and the dev profile set, so the whole surface is reachable —
    # config refuses that combination anywhere but dev (doc 05 §5.5).
    settings = load_service_settings(
        None,
        profile="dev",
        api={"enable_raw_sql": True},
        observability={"metrics_enabled": True},
    )

    def _selector_loop() -> asyncio.AbstractEventLoop:
        # Async psycopg cannot use the Windows Proactor loop. The fake driver
        # does not care, but keeping the same loop as the real launcher means
        # this demo exercises the real startup path.
        return asyncio.SelectorEventLoop(selectors.SelectSelector())

    loop_factory = _selector_loop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        foundation = runner.run(build_foundation())
        app = create_app(settings, foundation=foundation)

        host, port = settings.server.host, settings.server.port
        print("\n  pgfoundation DEMO — in-memory fake driver, no database\n")
        print(f"    docs      http://{host}:{port}/docs")
        print(f"    health    http://{host}:{port}/health")
        print(f"    metrics   http://{host}:{port}/metrics")
        print("    sources   main, analytics")
        print("\n    Import curls/pgfoundation.postman_collection.json and point")
        print(f"    baseUrl at http://{host}:{port}\n")

        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on")
        )
        runner.run(server.serve())


if __name__ == "__main__":
    main()
