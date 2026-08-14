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

## Demo objects

The examples reference these. Create them once against your database —
**the foundation owns no schema and will not create them for you**:

```sql
CREATE TABLE demo_orders (id int primary key, customer text, amount numeric, status text);
INSERT INTO demo_orders VALUES
  (1,'alice',19.90,'paid'),(3,'bob',42.00,'paid'),(4,'bob',7.50,'paid');

CREATE VIEW v_paid_orders AS
  SELECT id, customer, amount FROM demo_orders WHERE status='paid';

CREATE FUNCTION fn_orders_by_customer(cust text)
  RETURNS TABLE(id int, amount numeric) LANGUAGE sql AS $$
    SELECT id, amount FROM demo_orders WHERE customer = cust ORDER BY id; $$;

CREATE PROCEDURE sp_customer_stats(
    IN cust text, INOUT order_count int DEFAULT NULL, INOUT total numeric DEFAULT NULL)
  LANGUAGE plpgsql AS $$ BEGIN
    SELECT count(*), COALESCE(sum(amount),0) INTO order_count, total
    FROM demo_orders WHERE customer = cust; END; $$;

-- for the interval / fetch-since examples
CREATE TABLE demo_readings (ts timestamptz, id bigserial, meter_id int, kwh numeric);
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
       "name": "v_paid_orders",
       "columns": [
         "id",
         "customer",
         "amount"
       ],
       "filters": [
         {
           "column": "customer",
           "op": "eq",
           "value": "bob"
         }
       ],
       "order_by": [
         {
           "column": "id"
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
  -d '{"name": "fn_orders_by_customer", "args": {"cust": "bob"}}'
```

### procedure

Call a stored procedure by name

Authorized as `write` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/procedure' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "sp_customer_stats", "args": ["bob", null, null]}'
```

### interval

Time-bucketed fetch over a half-open [start, end) range

Authorized as `read` on the data source.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/interval' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{
       "source": "demo_readings",
       "time_column": "ts",
       "start": "2026-08-13T00:00:00Z",
       "end": "2026-08-14T00:00:00Z",
       "every": "15 minutes",
       "metrics": [
         {
           "column": "kwh",
           "func": "sum"
         }
       ],
       "group_by": [
         "meter_id"
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
       "sql": "SELECT ts, id, kwh FROM demo_readings WHERE meter_id = %(m)s",
       "params": {
         "m": 1
       },
       "cursor": {
         "order_by": [
           "ts",
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
       "sql": "INSERT INTO demo_orders (id, customer, amount, status) VALUES (%(id)s, %(c)s, %(a)s, 'paid')",
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
       "table": "demo_orders",
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
             "name": "v_paid_orders",
             "limit": 50
           }
         },
         {
           "id": "totals",
           "datasource": "$DS",
           "op": "function",
           "payload": {
             "name": "fn_orders_by_customer",
             "args": {
               "cust": "bob"
             }
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
             "name": "v_paid_orders",
             "limit": 10
           }
         },
         {
           "id": "s1",
           "datasource": "$DS",
           "op": "view",
           "payload": {
             "name": "v_paid_orders",
             "limit": 10
           }
         },
         {
           "id": "s2",
           "datasource": "$DS",
           "op": "view",
           "payload": {
             "name": "v_paid_orders",
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
           "id": "debit",
           "datasource": "$DS",
           "op": "procedure",
           "payload": {
             "name": "sp_customer_stats",
             "args": [
               "bob",
               null,
               null
             ]
           }
         },
         {
           "id": "credit",
           "datasource": "$DS",
           "op": "procedure",
           "payload": {
             "name": "sp_customer_stats",
             "args": [
               "alice",
               null,
               null
             ]
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
       "name": "v_paid_orders",
       "columns": [
         "id",
         "customer",
         "amount"
       ],
       "filters": [
         {
           "column": "customer",
           "op": "eq",
           "value": "bob"
         }
       ],
       "order_by": [
         {
           "column": "id"
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
  -d '{"name": "fn_orders_by_customer", "args": {"cust": "bob"}}'
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
       "sql": "SELECT id, customer, amount FROM demo_orders WHERE status = %(s)s LIMIT %(n)s",
       "params": {
         "s": "paid",
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
       "source": "demo_readings",
       "time_column": "ts",
       "start": "2026-08-13T00:00:00Z",
       "end": "2026-08-14T00:00:00Z",
       "every": "15 minutes",
       "metrics": [
         {
           "column": "kwh",
           "func": "sum"
         }
       ],
       "group_by": [
         "meter_id"
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
       "sql": "SELECT id, customer, amount FROM demo_orders WHERE status = %(s)s LIMIT %(n)s",
       "params": {
         "s": "paid",
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
       "sql": "UPDATE demo_orders SET status = %(s)s WHERE id = %(id)s",
       "params": {
         "s": "shipped",
         "id": 1
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
  -d '{"name": "v_paid_orders; DROP TABLE demo_orders"}'
```

### 400 — operator outside the whitelist

`op` is a closed enum; an unknown operator never reaches SQL.

```bash
curl -sS -X POST '$BASE/v1/datasources/$DS/view' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "v_paid_orders", "filters": [{"column": "id", "op": "; DROP", "value": 1}]}'
```

### 404 — unknown data source

Only configured, named sources are addressable. Clients never send a DSN.

```bash
curl -sS -X POST '$BASE/v1/datasources/no-such-source/view' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: $(uuidgen)' \
  -d '{"name": "v_paid_orders"}'
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
