# ADR-004 — One batch endpoint with pluggable execution strategies

**Status:** Accepted
**Date:** 2026-08-13
**Serves:** requirement 4 (parallel calls, batch calls, throughput)

## Context

The Django service executes exactly one operation per HTTP request, serially. The
Django design docs specify a `POST .../transaction` endpoint (ordered atomic
batch) and a `/v1/batch/jobs` async job API; neither was built.

Three distinct client needs exist, and they have genuinely different semantics:

1. **Independent operations, want them fast** — a dashboard filling six panels
   from two databases. Wants concurrency; does not want atomicity.
2. **Ordered dependent statements over a high-latency link** — wants ordering and
   few round-trips; does not need atomicity.
3. **All-or-nothing writes** — wants atomicity and correct retry on serialization
   failure; ordering is implied.

## Decision

**One endpoint, `POST /v1/batch`, with a `mode` selecting an execution Strategy.**

| Mode | Connections | Mechanism | Atomic |
|------|-------------|-----------|--------|
| `parallel` (default) | N, bounded | semaphore-bounded `asyncio.gather`, `return_exceptions=True` | no |
| `sequential` | 1, pinned | psycopg **pipeline mode** — N statements, ~1 round-trip | no |
| `transactional` | 1, pinned | one `UnitOfWork` inside `run_transaction(...)`, retried whole on `40001`/`40P01` | **yes** |

Supporting decisions:

- **Items are Commands** — `{id, datasource, op, payload}` — dispatched through
  the operation registry of ADR-003. No separate batch switch.
- **Items may target different data sources.** In `parallel` mode they then use
  independent pools, which is where most of the win comes from.
- **Partial success is a `200`** with per-item `status`. `207 Multi-Status` was
  rejected: poorly handled by clients, and the envelope is already unambiguous.
- **Results are ordered by request position**, regardless of completion order.
- **Every fan-out is bounded** by `batch.max_concurrency`, clamped by the global
  and per-datasource admission semaphores. `batch.max_items` bounds the list.
- **Streamable operations are not batch-dispatchable.** A stream pins a connection
  for its whole lifetime; N streams in one batch would hold N connections hostage
  to the slowest reader.
- **The async job API stays deferred.** It needs durable job state, i.e. a table,
  and the foundation owns no tables.

## Options considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **One endpoint, mode-selected strategy** | One contract, one validation path, one envelope; new modes are new classes; matches how clients actually think ("run these, atomically or not") | The request body carries a mode flag | **Chosen** |
| Three endpoints (`/batch/parallel`, `/batch/sequential`, `/batch/transaction`) | Each URL self-documenting | Three near-identical validation and envelope paths; the shared 90% gets copied; changing the envelope means changing three places | Rejected |
| Only a transactional endpoint (as the Django docs specified) | Simplest | Forces a transaction on read fan-out, which is the *most common* case and gains nothing from atomicity while costing a pinned connection and lost parallelism | Rejected |
| Client-side parallelism (client issues N HTTP calls) | Zero server work | Pays HTTP + TLS + auth + validation N times; gives the server no way to bound total in-flight DB work — the exact failure mode admission control exists to prevent | Rejected as the *only* option; still supported, since unary endpoints remain |
| GraphQL-style query batching | Familiar shape | Brings a query language and a resolver model into a layer whose entire security posture rests on *not* accepting client-authored queries | Rejected |

## Consequences

- **Positive:** all three client needs are served; the `POST .../transaction`
  endpoint the Django docs promised is delivered as `mode: transactional`, with
  correct transaction-level retry per the inherited ADR-010.
- **Positive:** HTTP, TLS, auth and validation costs are amortized over N
  operations.
- **Positive:** the server, not the client, decides how much concurrency the
  database receives.
- **Negative / mitigation — partial success is more complex for clients.** The
  envelope is explicit (`ok_count`, `error_count`, per-item `status`), documented
  with examples, and `fail_fast: true` is available for clients that prefer
  all-or-nothing reporting without transactional semantics.
- **Negative / mitigation — `transactional` bodies may run more than once** (the
  retry runner re-executes the whole body). This is inherited behavior and is
  documented at the endpoint, not just in an ADR.
- **Negative / mitigation — a batch is a bigger blast radius.** A single request
  can now consume many connections. That is exactly why the bounds in
  [06 §6.6](../06-concurrency-batch-parallel.md) are non-optional and
  config-validated against pool sizes.
