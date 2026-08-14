#!/usr/bin/env python
"""Generate the Postman collection and curl reference FROM the live OpenAPI.

    python curls/generate.py

Hand-written API examples drift the moment a route or DTO changes. These are
built by introspecting the actual application, so a capability that exists in
the operations table appears here, and one that does not, does not.

Outputs (both regenerable, never edited by hand):

    curls/pgfoundation.postman_collection.json   — import into Postman
    curls/CURLS.md                               — copy-paste reference

Example request bodies are curated here rather than derived from the schema:
a schema tells you a field is `list[Any]`, not that a sensible value is
``[["alice", 19.90]]``. The route list stays generated; only the payloads are
authored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1] / "postgresqlmodule" / "code"
sys.path.insert(0, str(CODE_ROOT))

OUT_DIR = Path(__file__).parent

# --------------------------------------------------------------------------- #
# Curated example bodies, keyed by the operation id in the registry.
# --------------------------------------------------------------------------- #
EXAMPLES: dict[str, dict] = {
    "view": {
        "name": "v_paid_orders",
        "columns": ["id", "customer", "amount"],
        "filters": [{"column": "customer", "op": "eq", "value": "bob"}],
        "order_by": [{"column": "id"}],
        "limit": 100,
    },
    "function": {"name": "fn_orders_by_customer", "args": {"cust": "bob"}},
    "procedure": {"name": "sp_customer_stats", "args": ["bob", None, None]},
    "interval": {
        "source": "demo_readings",
        "time_column": "ts",
        "start": "2026-08-13T00:00:00Z",
        "end": "2026-08-14T00:00:00Z",
        "every": "15 minutes",
        "metrics": [{"column": "kwh", "func": "sum"}],
        "group_by": ["meter_id"],
    },
    "fetch_since": {
        "sql": "SELECT ts, id, kwh FROM demo_readings WHERE meter_id = %(m)s",
        "params": {"m": 1},
        "cursor": {"order_by": ["ts", "id"], "after": None},
        "limit": 1000,
    },
    "execute_many": {
        "sql": "INSERT INTO demo_orders (id, customer, amount, status) "
        "VALUES (%(id)s, %(c)s, %(a)s, 'paid')",
        "params_seq": [
            {"id": 101, "c": "carol", "a": "12.00"},
            {"id": 102, "c": "dan", "a": "8.25"},
        ],
    },
    "copy": {
        "table": "demo_orders",
        "rows": [[201, "erin", "31.00", "paid"], [202, "frank", "5.50", "paid"]],
    },
    "query": {
        "sql": "SELECT id, customer, amount FROM demo_orders WHERE status = %(s)s LIMIT %(n)s",
        "params": {"s": "paid", "n": 10},
    },
    "execute": {
        "sql": "UPDATE demo_orders SET status = %(s)s WHERE id = %(id)s",
        "params": {"s": "shipped", "id": 1},
    },
}

STREAM_NOTE = (
    "Streams NDJSON (one JSON object per line). Send "
    "`Accept: text/event-stream` for SSE instead.\n\n"
    "A mid-stream failure cannot change the HTTP status — the headers are "
    "already sent — so it arrives as a final "
    '`{"error": {...}, "partial": true}` line. **Check the last line.**'
)

BATCH_EXAMPLES = [
    (
        "parallel — fan out across data sources",
        "The default. Independent operations run concurrently against independent "
        "pools. Partial success is normal: HTTP stays 200 and each item carries its "
        "own `status`.",
        {
            "mode": "parallel",
            "max_concurrency": 8,
            "items": [
                {
                    "id": "orders",
                    "datasource": "{{datasource}}",
                    "op": "view",
                    "payload": {"name": "v_paid_orders", "limit": 50},
                },
                {
                    "id": "totals",
                    "datasource": "{{datasource}}",
                    "op": "function",
                    "payload": {"name": "fn_orders_by_customer", "args": {"cust": "bob"}},
                },
            ],
        },
    ),
    (
        "sequential — ordered, pipelined",
        "One pinned connection, statements pipelined. Order is guaranteed and the "
        "HTTP/validation cost is paid once. Not atomic — use `transactional` for "
        "all-or-nothing. All items must target the same data source.",
        {
            "mode": "sequential",
            "items": [
                {
                    "id": f"s{i}",
                    "datasource": "{{datasource}}",
                    "op": "view",
                    "payload": {"name": "v_paid_orders", "limit": 10},
                }
                for i in range(3)
            ],
        },
    ),
    (
        "transactional — all or nothing",
        "One UnitOfWork for every item, retried **whole** on serialization failure "
        "(40001) or deadlock (40P01).\n\n"
        "**The body may run more than once**, so items must be free of "
        "non-database side effects. All items must target the same data source.",
        {
            "mode": "transactional",
            "items": [
                {
                    "id": "debit",
                    "datasource": "{{datasource}}",
                    "op": "procedure",
                    "payload": {"name": "sp_customer_stats", "args": ["bob", None, None]},
                },
                {
                    "id": "credit",
                    "datasource": "{{datasource}}",
                    "op": "procedure",
                    "payload": {"name": "sp_customer_stats", "args": ["alice", None, None]},
                },
            ],
        },
    ),
]

#: Requests that demonstrate a guarantee rather than a happy path. Worth shipping:
#: "prove the injection boundary holds" is a question every reviewer asks.
NEGATIVE_CASES = [
    (
        "400 — SQL injection in an object name",
        "The identifier is validated, never interpolated. This is the security "
        "boundary the whole name-based surface rests on.",
        "view",
        {"name": "v_paid_orders; DROP TABLE demo_orders"},
    ),
    (
        "400 — operator outside the whitelist",
        "`op` is a closed enum; an unknown operator never reaches SQL.",
        "view",
        {"name": "v_paid_orders", "filters": [{"column": "id", "op": "; DROP", "value": 1}]},
    ),
    (
        "404 — unknown data source",
        "Only configured, named sources are addressable. Clients never send a DSN.",
        "view",
        {"name": "v_paid_orders"},
    ),
]


def build_app():
    from pgfoundation_api.app import create_app
    from pgfoundation_api.settings import ServiceSettings

    # dev profile + raw SQL on, so the trusted operations are registered and
    # therefore documented. A production deployment omits them entirely.
    return create_app(ServiceSettings(profile="dev", api={"enable_raw_sql": True}))


def hdr(key: str, value: str, disabled: bool = False) -> dict:
    out = {"key": key, "value": value}
    if disabled:
        out["disabled"] = True
    return out


AUTH_HEADERS = [
    hdr("X-Request-Id", "{{$guid}}"),
    hdr(
        "x-client-cert-subject",
        "{{callerSubject}}",
        disabled=True,
    ),
    hdr("Authorization", "Bearer {{apiKey}}", disabled=True),
]


def request(
    name: str, method: str, path: str, *, body: dict | None = None, doc: str = "", accept: str = ""
) -> dict:
    headers = list(AUTH_HEADERS)
    if body is not None:
        headers.insert(0, hdr("Content-Type", "application/json"))
    if accept:
        headers.insert(0, hdr("Accept", accept))

    raw_path = path.lstrip("/").split("/")
    item: dict = {
        "name": name,
        "request": {
            "method": method,
            "header": headers,
            "url": {
                "raw": "{{baseUrl}}" + path,
                "host": ["{{baseUrl}}"],
                "path": raw_path,
            },
            "description": doc,
        },
        "response": [],
    }
    if body is not None:
        item["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    return item


def main() -> int:
    app = build_app()
    spec = app.openapi()
    paths = set(spec["paths"])

    from pgfoundation_api.api.v1.operations import OPERATIONS

    ops_folder: list[dict] = []
    stream_folder: list[dict] = []
    trusted_folder: list[dict] = []

    for op in OPERATIONS:
        example = EXAMPLES.get(op.op)
        if example is None:
            print(f"  ! no curated example for {op.op!r} — skipped", file=sys.stderr)
            continue
        route = f"/v1/datasources/{{{{datasource}}}}/{op.suffix}"
        doc = f"{op.summary}\n\nAuthorized as `{op.action}` on the data source."
        entry = request(op.op, "POST", route, body=example, doc=doc)
        (trusted_folder if op.trusted_only else ops_folder).append(entry)

        if op.streamable:
            stream_folder.append(
                request(
                    f"{op.op} (stream)",
                    "POST",
                    route + "/stream",
                    body=example,
                    doc=f"{op.summary}\n\n{STREAM_NOTE}",
                    accept="application/x-ndjson",
                )
            )

    batch_folder = [
        request(name, "POST", "/v1/batch", body=body, doc=doc)
        for name, doc, body in BATCH_EXAMPLES
    ]

    negative_folder = []
    for name, doc, op_id, body in NEGATIVE_CASES:
        source = "no-such-source" if name.startswith("404") else "{{datasource}}"
        negative_folder.append(
            request(name, "POST", f"/v1/datasources/{source}/{op_id}", body=body, doc=doc)
        )

    admin_folder = [
        request(
            "health (unversioned)",
            "GET",
            "/health",
            doc="Liveness. 200 while the process is up, whatever the databases say — "
            "a probe that fails on database trouble kills a process that would "
            "have recovered.",
        ),
        request(
            "ready (unversioned)",
            "GET",
            "/ready",
            doc="Readiness. 503 when no data source is reachable. **Point your "
            "orchestrator here**, not at the /v1 alias, so probes survive an API "
            "version retirement.",
        ),
        request("metrics", "GET", "/metrics", doc="Prometheus scrape. Empty unless "
                "`observability.metrics_enabled` is true."),
        request("datasources", "GET", "/v1/datasources", doc="List configured sources."),
        request("health (v1 alias)", "GET", "/v1/health", doc="Parity alias."),
        request("ready (v1 alias)", "GET", "/v1/ready", doc="Parity alias."),
    ]

    if "/v1/datasources/{name}/subscribe/{channel}" in paths:
        stream_folder.append(
            request(
                "subscribe (LISTEN/NOTIFY, SSE)",
                "GET",
                "/v1/datasources/{{datasource}}/subscribe/{{channel}}",
                doc="Server-Sent Events fed by LISTEN/NOTIFY. Holds a connection for "
                "the subscription's lifetime — target a data source whose pool can "
                "afford it.",
                accept="text/event-stream",
            )
        )

    collection = {
        "info": {
            "name": "pgfoundation (FastAPI)",
            "description": (
                "PostgreSQL Database Foundation Server.\n\n"
                "**Generated** from the live OpenAPI document by "
                "`curls/generate.py` — do not edit by hand; re-run instead.\n\n"
                "### Set up\n"
                "1. Set `baseUrl` (default `http://127.0.0.1:8600`) and `datasource`.\n"
                "2. Auth headers are **disabled by default**, matching "
                "`auth.enabled: false`. Enable `x-client-cert-subject` (mesh identity) "
                "or `Authorization` (token) once the deployment turns auth on.\n"
                "3. The trusted raw-SQL folder only works when "
                "`api.enable_raw_sql: true`; otherwise those routes are not "
                "registered at all and return 404.\n\n"
                "`curls/CURLS.md` has the same requests as copy-paste curl, plus "
                "the SQL needed to create the demo objects."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "baseUrl", "value": "http://127.0.0.1:8600"},
            {"key": "datasource", "value": "main"},
            {"key": "channel", "value": "orders_changed"},
            {"key": "callerSubject", "value": "orders-api"},
            {"key": "apiKey", "value": ""},
        ],
        "item": [
            {
                "name": "1 · Governed invocation (recommended)",
                "description": "Name a database object and pass parameters. **No SQL "
                "crosses the wire**, so there is no injection surface and no "
                "arbitrary-table access.",
                "item": ops_folder,
            },
            {
                "name": "2 · Batch",
                "description": "N operations in one request, with the execution "
                "semantics chosen per request.",
                "item": batch_folder,
            },
            {
                "name": "3 · Streaming",
                "description": "Constant-memory reads and live feeds.",
                "item": stream_folder,
            },
            {
                "name": "4 · Admin & probes",
                "description": "Unversioned operational surface plus the v1 aliases.",
                "item": admin_folder,
            },
            {
                "name": "5 · Advanced / trusted (raw SQL)",
                "description": "Registered ONLY when `api.enable_raw_sql: true`. "
                "Config refuses that combination with auth disabled outside the dev "
                "profile — raw SQL must never sit on an unauthenticated surface.",
                "item": trusted_folder,
            },
            {
                "name": "6 · Guarantees (expected failures)",
                "description": "These are *supposed* to fail. They demonstrate the "
                "security and validation boundaries.",
                "item": negative_folder,
            },
        ],
    }

    out_json = OUT_DIR / "pgfoundation.postman_collection.json"
    out_json.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")

    write_curls(collection)

    total = sum(len(f["item"]) for f in collection["item"])
    print(f"{out_json.name:44} {out_json.stat().st_size / 1024:6.1f} KB")
    curls = OUT_DIR / "CURLS.md"
    print(f"{curls.name:44} {curls.stat().st_size / 1024:6.1f} KB")
    print(f"{total} requests across {len(collection['item'])} folders")
    return 0


def write_curls(collection: dict) -> None:
    """The same requests as copy-paste curl."""
    lines: list[str] = [
        "# pgfoundation — curl reference",
        "",
        "**Generated** by `curls/generate.py` from the live OpenAPI document.",
        "Re-run it rather than editing this file.",
        "",
        "Postman can import any of these directly: **Import → Raw text → paste**.",
        "For the whole API at once, import "
        "`pgfoundation.postman_collection.json` instead.",
        "",
        "```bash",
        "BASE=http://127.0.0.1:8600",
        "DS=main            # a configured data-source name",
        "```",
        "",
        "## Authentication",
        "",
        "Auth is **disabled by default** (`auth.enabled: false`), so the examples",
        "below carry no credential. Once a deployment turns it on, add whichever",
        "provider it uses:",
        "",
        "```bash",
        "# mesh identity — the header must be set by the mesh/ingress and stripped",
        "# at the edge, or impersonation is a one-line curl",
        "-H 'x-client-cert-subject: orders-api'",
        "",
        "# or a per-service key",
        "-H 'Authorization: Bearer $PGF_API_KEY'",
        "```",
        "",
        "A caller is authorized per `datasource x action`, so a read-only caller",
        "gets 403 on `/procedure` even for a source it can read.",
        "",
        "## Demo objects",
        "",
        "The examples reference these. Create them once against your database —",
        "**the foundation owns no schema and will not create them for you**:",
        "",
        "```sql",
        "CREATE TABLE demo_orders (id int primary key, customer text, amount numeric, status text);",
        "INSERT INTO demo_orders VALUES",
        "  (1,'alice',19.90,'paid'),(3,'bob',42.00,'paid'),(4,'bob',7.50,'paid');",
        "",
        "CREATE VIEW v_paid_orders AS",
        "  SELECT id, customer, amount FROM demo_orders WHERE status='paid';",
        "",
        "CREATE FUNCTION fn_orders_by_customer(cust text)",
        "  RETURNS TABLE(id int, amount numeric) LANGUAGE sql AS $$",
        "    SELECT id, amount FROM demo_orders WHERE customer = cust ORDER BY id; $$;",
        "",
        "CREATE PROCEDURE sp_customer_stats(",
        "    IN cust text, INOUT order_count int DEFAULT NULL, INOUT total numeric DEFAULT NULL)",
        "  LANGUAGE plpgsql AS $$ BEGIN",
        "    SELECT count(*), COALESCE(sum(amount),0) INTO order_count, total",
        "    FROM demo_orders WHERE customer = cust; END; $$;",
        "",
        "-- for the interval / fetch-since examples",
        "CREATE TABLE demo_readings (ts timestamptz, id bigserial, meter_id int, kwh numeric);",
        "```",
        "",
        "---",
        "",
    ]

    for folder in collection["item"]:
        lines.append(f"## {folder['name']}")
        lines.append("")
        if folder.get("description"):
            lines.append(folder["description"])
            lines.append("")
        for item in folder["item"]:
            lines.append(f"### {item['name']}")
            lines.append("")
            if item["request"].get("description"):
                lines.append(item["request"]["description"])
                lines.append("")
            lines.append("```bash")
            lines.append(_curl(item))
            lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")

    lines += [
        "## Notes",
        "",
        "- **`numeric` is a JSON string** (`\"42.00\"`), never a float — exact",
        "  precision is a wire contract. Parse it as a decimal.",
        "- **`row_format: \"tuple\"`** on any read returns `rows` as arrays plus a",
        "  `columns` list: fewer allocations and a smaller payload on wide results.",
        "  `columns` is *absent* in the default `dict` format.",
        "- **Batch partial success returns 200.** Check each item's `status`; only a",
        "  whole-batch failure produces a non-200.",
        "- **Large unary reads return 413** naming the `/stream` route rather than",
        "  materializing an unbounded result.",
        "",
    ]
    (OUT_DIR / "CURLS.md").write_text("\n".join(lines), encoding="utf-8")


def _curl(item: dict) -> str:
    req = item["request"]
    url = req["url"]["raw"].replace("{{baseUrl}}", "$BASE").replace("{{datasource}}", "$DS")
    url = url.replace("{{channel}}", "orders_changed")

    parts = [f"curl -sS -X {req['method']} '{url}'"]
    if req["method"] == "GET" and "stream" in item["name"].lower():
        parts[0] = parts[0].replace("curl -sS", "curl -sS -N")
    for header in req["header"]:
        if header.get("disabled"):
            continue
        value = header["value"].replace("{{$guid}}", "$(uuidgen)")
        parts.append(f"  -H '{header['key']}: {value}'")
    if "body" in req:
        body = req["body"]["raw"].replace("{{datasource}}", "$DS")
        compact = json.dumps(json.loads(body.replace("$DS", "main")))
        if len(compact) <= 90:
            parts.append(f"  -d '{compact}'")
        else:
            parts.append("  -d '" + body.replace("\n", "\n     ") + "'")
    return " \\\n".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
