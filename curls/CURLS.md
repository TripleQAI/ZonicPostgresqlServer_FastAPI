# pgfoundation — curl reference

**Generated** by `curls/generate.py` from the live OpenAPI document.
Re-run it rather than editing this file.

Postman can import any of these directly: **Import → Raw text → paste**.
For the whole API at once, import `pgfoundation.postman_collection.json` instead.

```bash
BASE=http://127.0.0.1:8600
DS=main            # a configured data-source name
```

## Authentication

Auth is **disabled by default** (`auth.enabled: false`), so the examples
below carry no credential. Once a deployment turns it on, add whichever
provider it uses:

```bash
# mesh identity — the header must be set by the mesh/ingress and stripped
# at the edge, or impersonation is a one-line curl
-H 'x-client-cert-subject: orders-api'

# or a per-service key
-H 'Authorization: Bearer $PGF_API_KEY'
```

A caller is authorized per `datasource x action`, so a read-only caller
gets 403 on `/procedure` even for a source it can read.

## What the examples run against

**Read requests work as-is** against the deployed `main` data source
(Tiger Cloud `tsdb`) — no setup needed. They use objects that are really
there: `agentBuilder_agentworkflow`, `django_migrations`,
`aiplatform_node_execution_log`, and the built-in `now()`.

### Mixed-case relations — read this before writing raw SQL

Most tables in this schema were created by Django with a **camelCase**
name, e.g. `agentBuilder_agentworkflow` (capital **B**). PostgreSQL folds
an *unquoted* identifier to lower case, so:

```sql
-- fails: the server looks for `agentbuilder_agentworkflow`
SELECT * FROM public.agentBuilder_agentworkflow LIMIT 10;

-- works: quoted, so the case is preserved
SELECT * FROM public."agentBuilder_agentworkflow" LIMIT 10;
```

The **name-based operations quote identifiers for you**, so `/view`,
`/interval` and friends take the plain name and just work:

```json
{"name": "agentBuilder_agentworkflow", "schema": "public", "limit": 10}
```

### Scratch table for the write examples

`execute`, `execute_many`, `copy` and the transactional batch deliberately
do **not** touch application tables — importing a collection should never
modify live data on the first click. Create the scratch table once:

```sql
CREATE TABLE IF NOT EXISTS pgf_demo_orders (
  id int PRIMARY KEY, customer text, amount numeric, status text);

-- only for the /procedure example; CALL needs a procedure that exists
CREATE OR REPLACE PROCEDURE pgf_demo_touch(IN k int)
  LANGUAGE plpgsql AS $$ BEGIN
    UPDATE pgf_demo_orders SET status = status WHERE id = k; END; $$;
```

---

## 1 · Governed invocation (recommended)

Name a database object and pass parameters. **No SQL crosses the wire**, so there is no injection surface and no arbitrary-table access.

### view

Query a view or table by name with structured filters

Authorized as `read` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/view' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "name": "agentBuilder_agentworkflow",
       "schema": "public",
       "columns": [
         "id",
         "name",
         "display_name"
       ],
       "filters": [
         {
           "column": "name",
           "op": "ilike",
           "value": "%agent%"
         }
       ],
       "order_by": [
         {
           "column": "name"
         }
       ],
       "limit": 100
     }'
```

### function

Call a function by name

Authorized as `read` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/function' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "now", "scalar": true}'
```

### procedure

Call a stored procedure by name

Authorized as `write` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/procedure' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "pgf_demo_touch", "args": [1]}'
```

### interval

Time-bucketed fetch over a half-open [start, end) range

Authorized as `read` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/interval' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "source": "public.aiplatform_node_execution_log",
       "time_column": "started_at",
       "start": "2026-06-16T00:00:00Z",
       "end": "2026-07-29T00:00:00Z",
       "every": "1 day",
       "metrics": [
         {
           "column": "duration_ms",
           "func": "avg",
           "alias": "avg_ms"
         },
         {
           "column": "id",
           "func": "count",
           "alias": "runs"
         }
       ],
       "group_by": [
         "status"
       ]
     }'
```

### fetch_since

Resumable keyset page; returns rows plus the next watermark

Authorized as `read` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/fetch-since' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "sql": "SELECT started_at, id, node_name FROM public.aiplatform_node_execution_log",
       "params": {},
       "cursor": {
         "order_by": [
           "started_at",
           "id"
         ],
         "after": null
       },
       "limit": 1000
     }'
```

### execute_many

Batched parameterized writes

Authorized as `write` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/execute-many' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "sql": "INSERT INTO pgf_demo_orders (id, customer, amount, status) VALUES (%(id)s, %(c)s, %(a)s, 'paid')",
       "params_seq": [
         {
           "id": 101,
           "c": "carol",
           "a": "12.00"
         },
         {
           "id": 102,
           "c": "dan",
           "a": "8.25"
         }
       ]
     }'
```

### copy

Streaming COPY ingest

Authorized as `write` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/copy' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "table": "pgf_demo_orders",
       "rows": [
         [
           201,
           "erin",
           "31.00",
           "paid"
         ],
         [
           202,
           "frank",
           "5.50",
           "paid"
         ]
       ]
     }'
```

---

## 2 · Batch

N operations in one request, with the execution semantics chosen per request.

### parallel — fan out across data sources

The default. Independent operations run concurrently against independent pools. Partial success is normal: HTTP stays 200 and each item carries its own `status`.

```bash
curl -sS -X POST '$BASE/v1/batch' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "mode": "parallel",
       "max_concurrency": 8,
       "items": [
         {
           "id": "orders",
           "datasource": "$DS",
           "op": "view",
           "payload": {
             "name": "agentBuilder_agentworkflow",
             "schema": "public",
             "limit": 50
           }
         },
         {
           "id": "totals",
           "datasource": "$DS",
           "op": "function",
           "payload": {
             "name": "now",
             "scalar": true
           }
         }
       ]
     }'
```

### sequential — ordered, pipelined

One pinned connection, statements pipelined. Order is guaranteed and the HTTP/validation cost is paid once. Not atomic — use `transactional` for all-or-nothing. All items must target the same data source.

```bash
curl -sS -X POST '$BASE/v1/batch' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "mode": "sequential",
       "items": [
         {
           "id": "s0",
           "datasource": "$DS",
           "op": "view",
           "payload": {
             "name": "django_migrations",
             "schema": "public",
             "limit": 10
           }
         },
         {
           "id": "s1",
           "datasource": "$DS",
           "op": "view",
           "payload": {
             "name": "django_migrations",
             "schema": "public",
             "limit": 10
           }
         },
         {
           "id": "s2",
           "datasource": "$DS",
           "op": "view",
           "payload": {
             "name": "django_migrations",
             "schema": "public",
             "limit": 10
           }
         }
       ]
     }'
```

### transactional — all or nothing

One UnitOfWork for every item, retried **whole** on serialization failure (40001) or deadlock (40P01).

**The body may run more than once**, so items must be free of non-database side effects. All items must target the same data source.

```bash
curl -sS -X POST '$BASE/v1/batch' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "mode": "transactional",
       "items": [
         {
           "id": "insert",
           "datasource": "$DS",
           "op": "execute",
           "payload": {
             "sql": "INSERT INTO pgf_demo_orders (id, customer, amount, status) VALUES (%(id)s, 'grace', '10.00', 'paid')",
             "params": {
               "id": 301
             }
           }
         },
         {
           "id": "update",
           "datasource": "$DS",
           "op": "execute",
           "payload": {
             "sql": "UPDATE pgf_demo_orders SET status = 'shipped' WHERE id = %(id)s",
             "params": {
               "id": 301
             }
           }
         }
       ]
     }'
```

---

## 3 · Streaming

Constant-memory reads and live feeds.

### view (stream)

Query a view or table by name with structured filters

Streams NDJSON (one JSON object per line). Send `Accept: text/event-stream` for SSE instead.

A mid-stream failure cannot change the HTTP status — the headers are already sent — so it arrives as a final `{"error": {...}, "partial": true}` line. **Check the last line.**

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/view/stream' \
  -H 'Accept: application/x-ndjson' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "name": "agentBuilder_agentworkflow",
       "schema": "public",
       "columns": [
         "id",
         "name",
         "display_name"
       ],
       "filters": [
         {
           "column": "name",
           "op": "ilike",
           "value": "%agent%"
         }
       ],
       "order_by": [
         {
           "column": "name"
         }
       ],
       "limit": 100
     }'
```

### function (stream)

Call a function by name

Streams NDJSON (one JSON object per line). Send `Accept: text/event-stream` for SSE instead.

A mid-stream failure cannot change the HTTP status — the headers are already sent — so it arrives as a final `{"error": {...}, "partial": true}` line. **Check the last line.**

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/function/stream' \
  -H 'Accept: application/x-ndjson' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "now", "scalar": true}'
```

### query (stream)

Run a read query (raw SQL — advanced)

Streams NDJSON (one JSON object per line). Send `Accept: text/event-stream` for SSE instead.

A mid-stream failure cannot change the HTTP status — the headers are already sent — so it arrives as a final `{"error": {...}, "partial": true}` line. **Check the last line.**

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/query/stream' \
  -H 'Accept: application/x-ndjson' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "sql": "SELECT id, name, display_name FROM public.\"agentBuilder_agentworkflow\" ORDER BY name LIMIT %(n)s",
       "params": {
         "n": 10
       }
     }'
```

### interval (stream)

Time-bucketed fetch over a half-open [start, end) range

Streams NDJSON (one JSON object per line). Send `Accept: text/event-stream` for SSE instead.

A mid-stream failure cannot change the HTTP status — the headers are already sent — so it arrives as a final `{"error": {...}, "partial": true}` line. **Check the last line.**

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/interval/stream' \
  -H 'Accept: application/x-ndjson' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "source": "public.aiplatform_node_execution_log",
       "time_column": "started_at",
       "start": "2026-06-16T00:00:00Z",
       "end": "2026-07-29T00:00:00Z",
       "every": "1 day",
       "metrics": [
         {
           "column": "duration_ms",
           "func": "avg",
           "alias": "avg_ms"
         },
         {
           "column": "id",
           "func": "count",
           "alias": "runs"
         }
       ],
       "group_by": [
         "status"
       ]
     }'
```

### subscribe (LISTEN/NOTIFY, SSE)

Server-Sent Events fed by LISTEN/NOTIFY. Holds a connection for the subscription's lifetime — target a data source whose pool can afford it.

```bash
curl -sS -X GET '$BASE/v1/datasources/$DS/subscribe/orders_changed' \
  -H 'Accept: text/event-stream' \
  -H 'X-Request-Id: $(uuidgen)'
```

---

## 4 · Admin & probes

Unversioned operational surface plus the v1 aliases.

### health (unversioned)

Liveness. 200 while the process is up, whatever the databases say — a probe that fails on database trouble kills a process that would have recovered.

```bash
curl -sS -X GET '$BASE/health' \
  -H 'X-Request-Id: $(uuidgen)'
```

### ready (unversioned)

Readiness. 503 when no data source is reachable. **Point your orchestrator here**, not at the /v1 alias, so probes survive an API version retirement.

```bash
curl -sS -X GET '$BASE/ready' \
  -H 'X-Request-Id: $(uuidgen)'
```

### metrics

Prometheus scrape. Empty unless `observability.metrics_enabled` is true.

```bash
curl -sS -X GET '$BASE/metrics' \
  -H 'X-Request-Id: $(uuidgen)'
```

### datasources

List configured sources.

```bash
curl -sS -X GET '$BASE/v1/datasources' \
  -H 'X-Request-Id: $(uuidgen)'
```

### health (v1 alias)

Parity alias.

```bash
curl -sS -X GET '$BASE/v1/health' \
  -H 'X-Request-Id: $(uuidgen)'
```

### ready (v1 alias)

Parity alias.

```bash
curl -sS -X GET '$BASE/v1/ready' \
  -H 'X-Request-Id: $(uuidgen)'
```

---

## 5 · Advanced / trusted (raw SQL)

Registered ONLY when `api.enable_raw_sql: true`. Config refuses that combination with auth disabled outside the dev profile — raw SQL must never sit on an unauthenticated surface.

### query

Run a read query (raw SQL — advanced)

Authorized as `read` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/query' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "sql": "SELECT id, name, display_name FROM public.\"agentBuilder_agentworkflow\" ORDER BY name LIMIT %(n)s",
       "params": {
         "n": 10
       }
     }'
```

### execute

Run a write command (raw SQL — advanced)

Authorized as `write` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/execute' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "sql": "UPDATE pgf_demo_orders SET status = %(s)s WHERE id = %(id)s",
       "params": {
         "s": "shipped",
         "id": 101
       }
     }'
```

---

## 6 · Guarantees (expected failures)

These are *supposed* to fail. They demonstrate the security and validation boundaries.

### 400 — SQL injection in an object name

The identifier is validated, never interpolated. This is the security boundary the whole name-based surface rests on.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/view' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "agentBuilder_agentworkflow; DROP TABLE auth_user"}'
```

### 400 — operator outside the whitelist

`op` is a closed enum; an unknown operator never reaches SQL.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/view' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "name": "agentBuilder_agentworkflow",
       "schema": "public",
       "filters": [
         {
           "column": "id",
           "op": "; DROP",
           "value": 1
         }
       ]
     }'
```

### 404 — unknown data source

Only configured, named sources are addressable. Clients never send a DSN.

```bash
curl -sS -X POST '$BASE/v1/datasources/no-such-source/view' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "django_migrations", "schema": "public"}'
```

---

## Notes

- **`numeric` is a JSON string** (`"42.00"`), never a float — exact
  precision is a wire contract. Parse it as a decimal.
- **`row_format: "tuple"`** on any read returns `rows` as arrays plus a
  `columns` list: fewer allocations and a smaller payload on wide results.
  `columns` is *absent* in the default `dict` format.
- **Batch partial success returns 200.** Check each item's `status`; only a
  whole-batch failure produces a non-200.
- **Large unary reads return 413** naming the `/stream` route rather than
  materializing an unbounded result.
