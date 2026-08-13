# 01 — Scope & Feature Parity

## 1.1 What the Django project is

`ZonicPostgresqlServer_DjangoApp` is a **PostgreSQL infrastructure layer**, not an
application. It owns no tables, ships no migrations, and has no business logic.
It exists so every downstream project shares one correct implementation of
connection handling, pooling, transactions, retries, streaming and configuration.

It is ~3,900 lines across two internal layers:

| Layer | Package | Role |
|-------|---------|------|
| **Core engine** | `pgfoundation` | Framework-free. Ports (`Protocol`s), value objects, error hierarchy, config, connection registry, psycopg adapter, access layer (UoW, invocation compilers, interval, watermark), resilience, observability. |
| **Service shell** | `pgfoundation_service` | Plain Django on ASGI. Async views, Pydantic DTOs, hand-built `apispec` OpenAPI document, error translation, auth seam, bootstrap singletons. |

The **cardinal rule** — the shell holds no data-access logic — is respected in the
existing code and is respected here. It is also what makes this port low-risk.

## 1.2 What is being rebuilt, and what is not

```
Core engine  ─────────────────────────►  ported ~unchanged  (see ADR-002)
                                          + additive extensions for batch/streaming
Service shell ────────────────────────►  rewritten on FastAPI  (see ADR-001)
                                          + declarative registry, batch layer
```

The core is already framework-free and passes an `import-linter` contract that
forbids Django anywhere inside it. So there is nothing Django-shaped to remove
from it — it moves across as-is. All of the rewrite budget goes to the shell,
where all four requirements are actually decided.

## 1.3 Feature parity matrix

Every capability of the Django project, and its disposition here. **Nothing is
dropped.**

### Core engine — reusable API

| Capability | Django (today) | FastAPI plan |
|------------|----------------|--------------|
| `query` / `one` / `scalar` | ✅ | ✅ ported |
| `execute` / `execute_many` | ✅ | ✅ ported; `execute_many` also exposed over HTTP (new) |
| `transaction(...)` Unit of Work + savepoints | ✅ | ✅ ported |
| `run_transaction(fn, retry=…)` (retry on 40001/40P01) | ✅ | ✅ ported; now reachable over HTTP via the transactional batch mode (new) |
| `stream(spec)` server-side cursor | ✅ core only | ✅ ported; now reachable over HTTP (new) |
| `listen(channel)` LISTEN/NOTIFY | ✅ core only | ✅ ported; now reachable over HTTP as SSE (new) |
| `interval(IntervalQuery)` time bucketing | ✅ core only | ✅ ported; now reachable over HTTP (new) |
| `fetch_since(spec, cursor)` keyset watermark | ✅ core only | ✅ ported; now reachable over HTTP (new) |
| `copy_stream(table, rows)` COPY ingest | ✅ core only | ✅ ported; now reachable over HTTP (new) |
| `datasource` / `names` / `health` | ✅ | ✅ ported; `health` fan-out parallelized |
| Governed invocation: `view` / `function` / `procedure` | ✅ | ✅ ported unchanged (the compilers are the security boundary — do not touch) |
| Error hierarchy + SQLSTATE mapping | ✅ | ✅ ported |
| Multi-datasource registry, per-source pools (bulkhead) | ✅ | ✅ ported |
| Circuit breaker per data source | ✅ | ✅ ported + made concurrency-safe (§06.9) |
| Metrics/log/trace ports with no-op defaults | ✅ | ✅ ported |
| `Repository` base | ✅ | ✅ ported |
| `FakeDriver` for DB-less tests | ✅ | ✅ ported |
| Config: dict / YAML / JSON, `${env:}` / `${secret:}` | ✅ | ✅ ported + extended (§05) |

### Service shell — HTTP surface

| Endpoint | Django | FastAPI plan |
|----------|--------|--------------|
| `POST /v1/datasources/{name}/view` | ✅ | ✅ generated from the registry |
| `POST /v1/datasources/{name}/function` | ✅ | ✅ generated |
| `POST /v1/datasources/{name}/procedure` | ✅ | ✅ generated |
| `POST /v1/datasources/{name}/query` (raw SQL, trusted) | ✅ | ✅ generated, gated behind config |
| `POST /v1/datasources/{name}/execute` (raw SQL, trusted) | ✅ | ✅ generated, gated behind config |
| `GET /v1/datasources` | ✅ | ✅ |
| `GET /v1/health` | ✅ | ✅ + `GET /v1/ready` split |
| `GET /openapi.json` | ✅ hand-built with `apispec` (75 LOC) | ✅ **free** from FastAPI |
| `GET /docs` | ✅ placeholder HTML page, no real UI | ✅ **real** Swagger UI + ReDoc, built in |
| Auth seam (disabled by default, fails closed) | ✅ | ✅ ported, moved to a `Depends` |
| Error → HTTP status mapping | ✅ | ✅ ported as exception handlers |
| Request-ID header | ✅ | ✅ ported as middleware |

### Designed in the Django docs but never built — now delivered

These are listed in the Django `documentations/08-service-shell.md` REST table and
in doc 16, but have **no implementation** in `views.py` / `urls.py`. The FastAPI
shell delivers them, because the registry makes each one a table row rather than a
new hand-written view:

| Capability | Status in Django | Plan |
|------------|------------------|------|
| `POST .../stream` (chunked NDJSON) | designed, not built | **built** (§06.7, ADR-006) |
| `POST .../interval` | designed, not built | **built** |
| `POST .../fetch-since` | designed, not built | **built** |
| `POST .../copy` (bulk ingest) | designed, not built | **built** |
| `GET .../subscribe/{channel}` (SSE) | designed, not built | **built** (ADR-006) |
| `POST .../transaction` (ordered atomic batch) | designed, not built | **built** as batch `mode: transactional` |
| Batch job API | designed, not built | **built** as the synchronous `/v1/batch` (async job queue stays deferred — §1.5) |
| `GET /metrics` Prometheus | designed, not built | **seam built**; exporter binding stays external per ADR-014 |
| `timeout_ms` per request | in the doc's example body, unimplemented | **built** (§06.8) |
| `row_format: dict \| tuple` | in the doc's example body, unimplemented | **built** (ADR-007) |
| Rate limiting | designed, not built | **built** as a config-driven concurrency limiter (§06.6) |

### New, driven by requirement 4

| Capability | Why |
|------------|-----|
| `POST /v1/batch` — N operations, one request | Removes per-call HTTP + TLS + auth overhead; lets the server choose the cheapest execution strategy. |
| Execution strategies: `parallel` / `sequential` / `transactional` | One endpoint, three concurrency semantics, chosen per request (§06.3). |
| Bounded fan-out with a global semaphore | Concurrency without moving the queue from the pool to the event loop (§06.6). |
| psycopg **pipeline mode** for sequential batches | N statements, ~1 round-trip (§07.3). |
| Cross-datasource parallelism | A batch touching 3 data sources uses 3 independent pools — natural bulkhead. |
| `POST .../execute-many` | Batched parameterized writes over HTTP. |

## 1.4 Non-goals (unchanged from the Django project)

Carried over verbatim, because they are still right:

- **Not an ORM, not a schema owner.** No models, no migrations, no DDL, no tables.
  The consuming project owns all schema.
- **Not a business-domain service.** No domain entities.
- **Not multi-engine.** PostgreSQL only. The `DriverPort` seam exists; a second
  engine is not a goal.
- **Semantic layer: deferred.** The foundation ships only the generic seams
  (composable `QuerySpec`, safe parameterization, materialized-view lifecycle via
  plain SQL, access-filter hook).
- **Time-series / TimescaleDB: deferred** behind the pluggable bucketing Strategy.
  Note that the named deployment target *is* Timescale Cloud
  ([12 §12.7](./12-deployment-targets.md)) — everything in this plan runs against
  it unchanged, and the seam stays a seam until measurement says otherwise.
- **End-user authN/authZ, tenancy and row-level policy stay with the higher-level
  APIs.** *Amended by [ADR-008](./adr/ADR-008-auth-boundary.md):* **caller**
  authentication and coarse datasource × action authorization are now **in scope**
  for the shared-service deployment shape — the seam stays, and ships with default
  providers so it can actually be switched on.
- **Observability is integrated, not built.** Ports with no-op defaults.
- **CDC / logical replication: reserved port only.**

## 1.5 Deliberately still deferred

| Item | Why |
|------|-----|
| **Asynchronous batch *job* API** (`202` + `job_id` + polling/webhook) | Needs durable job state, which means a table — and the foundation owns no tables. The synchronous `/v1/batch` plus streaming covers the realistic cases; the job API belongs to a consumer that owns its own job table. |
| **gRPC surface** | Real value for internal hot paths, but it is a second transport to keep at parity. The registry (§04) is transport-agnostic by construction, so gRPC servicers can be generated from the same table later without touching the core. |
| **Arrow / Arrow Flight** | Heavy optional dependency; the delivery-format Strategy seam is in place for it. |
| **Read/write routing (primary/replica)** | `role` is already in the config model but unused. Routing stays a Phase-5 item behind a `RoutingStrategy` seam. |

## 1.6 Traceability

| Requirement | Primary answer |
|-------------|----------------|
| **1 · Same features** | This document's parity matrix; the core is ported, not reimplemented; a parity test harness runs the same operation through library mode and service mode ([10 §10.4](./10-testing-and-delivery-plan.md)). |
| **2 · Low code, no hard-coding** | [04](./04-declarative-operation-registry.md) (one table → three surfaces) + [05](./05-configuration.md) (every operational value externalized, validated, fail-fast). |
| **3 · Design patterns** | [03](./03-design-patterns.md). |
| **4 · Performance / throughput / concurrency / parallel / batch** | [06](./06-concurrency-batch-parallel.md) + [07](./07-performance.md). |
