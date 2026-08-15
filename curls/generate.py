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
#
# Every READ example below runs as-is against the deployed `tsdb` data source
# (Tiger Cloud) — no setup, no imagined schema. They were previously
# written against demo objects that do not exist there, so an imported
# collection returned `relation ... does not exist` on almost every request.
#
# The WRITE examples (`execute`, `execute_many`, `copy`) deliberately do NOT
# point at the real application tables — an imported collection must not modify
# live data on first click. They target a scratch table; `CURLS.md` ships the
# one statement that creates it.
#
# Note `agentBuilder_agentworkflow`: the relation is MIXED CASE. In raw SQL it
# must be quoted (`public."agentBuilder_agentworkflow"`), because PostgreSQL
# folds unquoted identifiers to lower case. The name-based operations quote it
# for you — that is the whole point of using them.
# --------------------------------------------------------------------------- #
SCRATCH = "pgf_demo_orders"  # created by the setup SQL in CURLS.md

EXAMPLES: dict[str, dict] = {
    "view": {
        "name": "agentBuilder_agentworkflow",
        "schema": "public",
        "columns": ["id", "name", "display_name"],
        "filters": [{"column": "name", "op": "ilike", "value": "%agent%"}],
        "order_by": [{"column": "name"}],
        "limit": 100,
    },
    # `now()` is a built-in, so this works on any database. Swap in your own
    # set-returning function and drop `scalar` to get rows instead.
    "function": {"name": "now", "scalar": True},
    # No stored procedure ships with this schema — CALL needs one you own.
    "procedure": {"name": "pgf_demo_touch", "args": [1]},
    "interval": {
        "source": "public.aiplatform_node_execution_log",
        "time_column": "started_at",
        "start": "2026-06-16T00:00:00Z",
        "end": "2026-07-29T00:00:00Z",
        "every": "1 day",
        "metrics": [
            {"column": "duration_ms", "func": "avg", "alias": "avg_ms"},
            {"column": "id", "func": "count", "alias": "runs"},
        ],
        "group_by": ["status"],
    },
    "fetch_since": {
        "sql": "SELECT started_at, id, node_name FROM public.aiplatform_node_execution_log",
        "params": {},
        "cursor": {"order_by": ["started_at", "id"], "after": None},
        "limit": 1000,
    },
    "execute_many": {
        "sql": f"INSERT INTO {SCRATCH} (id, customer, amount, status) "
        "VALUES (%(id)s, %(c)s, %(a)s, 'paid')",
        "params_seq": [
            {"id": 101, "c": "carol", "a": "12.00"},
            {"id": 102, "c": "dan", "a": "8.25"},
        ],
    },
    "copy": {
        "table": SCRATCH,
        "rows": [[201, "erin", "31.00", "paid"], [202, "frank", "5.50", "paid"]],
    },
    # Raw SQL: the relation is mixed case, so it MUST be quoted here.
    "query": {
        "sql": 'SELECT id, name, display_name FROM public."agentBuilder_agentworkflow" '
        "ORDER BY name LIMIT %(n)s",
        "params": {"n": 10},
    },
    "execute": {
        "sql": f"UPDATE {SCRATCH} SET status = %(s)s WHERE id = %(id)s",
        "params": {"s": "shipped", "id": 101},
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
                    "payload": {
                        "name": "agentBuilder_agentworkflow",
                        "schema": "public",
                        "limit": 50,
                    },
                },
                {
                    "id": "totals",
                    "datasource": "{{datasource}}",
                    "op": "function",
                    "payload": {"name": "now", "scalar": True},
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
                    "payload": {
                        "name": "django_migrations",
                        "schema": "public",
                        "limit": 10,
                    },
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
                    "id": "insert",
                    "datasource": "{{datasource}}",
                    "op": "execute",
                    "payload": {
                        "sql": f"INSERT INTO {SCRATCH} (id, customer, amount, status)"
                        " VALUES (%(id)s, 'grace', '10.00', 'paid')",
                        "params": {"id": 301},
                    },
                },
                {
                    "id": "update",
                    "datasource": "{{datasource}}",
                    "op": "execute",
                    "payload": {
                        "sql": f"UPDATE {SCRATCH} SET status = 'shipped' WHERE id = %(id)s",
                        "params": {"id": 301},
                    },
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
        {"name": "agentBuilder_agentworkflow; DROP TABLE auth_user"},
    ),
    (
        "400 — operator outside the whitelist",
        "`op` is a closed enum; an unknown operator never reaches SQL.",
        "view",
        {
            "name": "agentBuilder_agentworkflow",
            "schema": "public",
            "filters": [{"column": "id", "op": "; DROP", "value": 1}],
        },
    ),
    (
        "404 — unknown data source",
        "Only configured, named sources are addressable. Clients never send a DSN.",
        "view",
        {"name": "django_migrations", "schema": "public"},
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
            {"key": "datasource", "value": "tsdb"},
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
        "DS=tsdb            # a configured data-source name (aliases are named",
        "                   # after the database they open)",
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
        "## What the examples run against",
        "",
        "**Read requests work as-is** against the deployed `main` data source",
        "(Tiger Cloud `tsdb`) — no setup needed. They use objects that are really",
        "there: `agentBuilder_agentworkflow`, `django_migrations`,",
        "`aiplatform_node_execution_log`, and the built-in `now()`.",
        "",
        "### Mixed-case relations — read this before writing raw SQL",
        "",
        "Most tables in this schema were created by Django with a **camelCase**",
        "name, e.g. `agentBuilder_agentworkflow` (capital **B**). PostgreSQL folds",
        "an *unquoted* identifier to lower case, so:",
        "",
        "```sql",
        "-- fails: the server looks for `agentbuilder_agentworkflow`",
        "SELECT * FROM public.agentBuilder_agentworkflow LIMIT 10;",
        "",
        "-- works: quoted, so the case is preserved",
        'SELECT * FROM public."agentBuilder_agentworkflow" LIMIT 10;',
        "```",
        "",
        "The **name-based operations quote identifiers for you**, so `/view`,",
        "`/interval` and friends take the plain name and just work:",
        "",
        "```json",
        '{"name": "agentBuilder_agentworkflow", "schema": "public", "limit": 10}',
        "```",
        "",
        "### Scratch table for the write examples",
        "",
        "`execute`, `execute_many`, `copy` and the transactional batch deliberately",
        "do **not** touch application tables — importing a collection should never",
        "modify live data on the first click. Create the scratch table once:",
        "",
        "```sql",
        f"CREATE TABLE IF NOT EXISTS {SCRATCH} (",
        "  id int PRIMARY KEY, customer text, amount numeric, status text);",
        "",
        "-- only for the /procedure example; CALL needs a procedure that exists",
        "CREATE OR REPLACE PROCEDURE pgf_demo_touch(IN k int)",
        "  LANGUAGE plpgsql AS $$ BEGIN",
        f"    UPDATE {SCRATCH} SET status = status WHERE id = k; END; $$;",
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
        compact = json.dumps(json.loads(body.replace("$DS", "tsdb")))
        if len(compact) <= 90:
            parts.append(f"  -d '{compact}'")
        else:
            parts.append("  -d '" + body.replace("\n", "\n     ") + "'")
    return " \\\n".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
