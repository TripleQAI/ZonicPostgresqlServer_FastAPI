# 11 — Observability: Instrumentation Points

**This document is comments only — no implementation.** It marks *where* the
system must emit signals and *what* those signals are called, so the
instrumentation can be added later without re-deciding any of it.

The inherited stance (ADR-014) is unchanged and worth restating, because it is
what keeps this document small:

> The foundation **instruments**; it does **not** build a telemetry pipeline.
> It emits through `LoggerPort` / `MetricsPort` / `TracerPort`, whose defaults are
> Null Objects. An external log/observability project binds the real adapters at
> the composition root and owns exporters, collectors, storage and dashboards.

So what the foundation owns is exactly this: **the instrumentation points and the
signal vocabulary**. That is a cross-project contract, which is why it is written
down before any code exists.

## 11.1 The marker convention

Instrumentation points are marked in the design's code sketches with a greppable
comment:

```python
# OBSERVE (METRIC): pgf_query_rows{datasource,op} — observe result.rowcount
# OBSERVE (TRACE):  span "pgf.operation" {op, datasource}
# OBSERVE (LOG):    query.slow when elapsed_ms > observability.slow_query_ms
```

| Tag | Meaning | Port |
|-----|---------|------|
| `OBSERVE (METRIC)` | A counter, histogram or gauge | `MetricsPort` |
| `OBSERVE (TRACE)` | A span to open, or an attribute/event to attach | `TracerPort` |
| `OBSERVE (LOG)` | A structured log event | `LoggerPort` |

`grep -rn "OBSERVE" postgresqlmodule/code` should list every outstanding
instrumentation point once implementation starts.

## 11.2 What is already emitted today

The ported core already meters through `MeteredPool` / `MeteredConnection`. These
four names are a **published contract — do not rename them**:

| Signal | Type | Labels | Emitted from |
|--------|------|--------|--------------|
| `pgf_query_duration_seconds` | histogram | `datasource` | `MeteredConnection.execute` |
| `pgf_query_errors_total` | counter | `datasource`, `type` | `MeteredConnection.execute` / `execute_many` |
| `pgf_pool_timeouts_total` | counter | `datasource` | `MeteredPool.acquire` |
| `pgf_pool_errors_total` | counter | `datasource` | `MeteredPool.acquire` |

Everything below is **additive**. One extension to an existing signal is proposed:
add an `op` label to `pgf_query_duration_seconds`, sourced from
`OperationSpec.op` ([04](./04-declarative-operation-registry.md)). It is a bounded
set of ~9 values and it turns a single undifferentiated latency curve into a
per-capability one, which is the difference between "the database is slow" and
"`interval` is slow."

## 11.3 Instrumentation points by layer

### L4 · Delivery (FastAPI shell)

| Point | Signals |
|-------|---------|
| Request in / out (middleware) | `METRIC` `pgf_http_requests_total{route,method,status,version}`, `pgf_http_request_duration_seconds{route,method}`, `pgf_http_requests_inflight{route}` · `TRACE` root span `http.request`, continuing any inbound trace context. The `version` label is what answers "is anyone still on v1?" before it is switched off ([09 §9.1b](./09-project-structure.md)) |
| Correlation id | `LOG` every event carries `request_id`; echoed as `X-Request-Id` (§11.7) |
| Request validation failure | `METRIC` `pgf_http_requests_total{status="400"}` · `LOG` `request.invalid` — **field names only, never field values** |
| Auth check (`Depends(authorize(...))`) | `METRIC` `pgf_auth_checks_total{action,result}` where result ∈ `allow`/`deny`/`unauthenticated` · `TRACE` span `auth.check` · `LOG` `auth.denied {action, resource, subject}` |
| Admission control (§6.6) | `METRIC` `pgf_admission_wait_seconds{scope}`, `pgf_admission_rejected_total{scope,datasource}`, `pgf_admission_slots_inuse{scope,datasource}` · `LOG` `admission.rejected {scope, waited_ms}` |
| Generated endpoint | `TRACE` span `pgf.operation {op, datasource}` · `METRIC` `pgf_query_rows{datasource,op}` |
| Response too large (413) | `METRIC` `pgf_response_truncated_total{route}` · `LOG` `response.too_large {rows, limit}` — this is the signal that a client should be moved to `/stream` |
| Error translation | `METRIC` counter by mapped status · `LOG` `request.failed {type, status}` |

### L4 · Batch ([06](./06-concurrency-batch-parallel.md))

| Point | Signals |
|-------|---------|
| Batch admitted | `METRIC` `pgf_batch_requests_total{mode}`, `pgf_batch_items{mode}` · `TRACE` span `pgf.batch {mode, items}` |
| Per item | `METRIC` `pgf_batch_item_duration_seconds{mode,op}`, `pgf_batch_items_total{mode,status}` · `TRACE` child span `pgf.batch.item {item_id, op, datasource}` |
| Achieved parallelism | `METRIC` `pgf_batch_concurrency_peak{mode}` — **the signal that proves requirement 4**: if peak concurrency never approaches the cap, the fan-out is being throttled somewhere and the parallel mode is not paying for itself |
| Item timeout | `LOG` `batch.item_timeout {item_id, op, budget_ms}` |
| Batch complete | `LOG` `batch.completed {mode, items, ok, errors, elapsed_ms}` · `METRIC` `pgf_batch_duration_seconds{mode}` |
| Transactional rollback | `LOG` `batch.rolled_back {items, failed_item_id, reason}` |

### L4 · Streaming ([ADR-006](./adr/ADR-006-streaming-delivery.md))

| Point | Signals |
|-------|---------|
| Stream opened / closed | `METRIC` `pgf_stream_active{datasource}` gauge · `TRACE` span `pgf.stream {op, datasource}` |
| Rows delivered | `METRIC` `pgf_stream_rows_total{datasource,op}` — **counter, not a span event per row** (§11.9) |
| Duration | `METRIC` `pgf_stream_duration_seconds{datasource,op}` |
| **Connection hold time** | `METRIC` `pgf_stream_connection_hold_seconds{datasource}` — the early-warning signal for pool starvation by long scans; a rising p99 here predicts `pgf_pool_timeouts_total` on the *other* routes sharing that pool |
| Termination | `METRIC` `pgf_stream_terminations_total{datasource,reason}` where reason ∈ `complete`/`client_disconnect`/`error`/`timeout` · `LOG` `stream.aborted {op, rows_sent, reason}` |
| Mid-stream error | `LOG` `stream.failed` — the client only sees the in-band final line, so this must be visible server-side |

### L3 · Facade

| Point | Signals |
|-------|---------|
| Operation dispatch | `TRACE` span attributes: `op`, `datasource`, `object` (view/function/procedure name) |
| Unknown data source | `METRIC` `pgf_unknown_datasource_total{name}` — **bounded by config, safe to label** · `LOG` `datasource.unknown` |

### L2 · Data access

| Point | Signals |
|-------|---------|
| Invocation compile | `TRACE` span `pgf.compile {op}` — normally negligible; worth a span only while validating the prepared-statement determinism claim ([07 §7.3](./07-performance.md)) |
| Identifier rejected | `METRIC` `pgf_unsafe_identifier_total{op}` · `LOG` `security.unsafe_identifier {op, field}` — **log the field name, never the offending value** (§11.8). This is a security signal: a spike means someone is probing the injection boundary |
| Statement execute | already covered by `pgf_query_duration_seconds` / `pgf_query_errors_total`; add `TRACE` span `pgf.db.execute` with `db.system=postgresql` |
| Slow statement | `LOG` `query.slow {datasource, op, object, elapsed_ms}` above `observability.slow_query_ms` |
| Transaction begin/commit/rollback | `METRIC` `pgf_transactions_total{datasource,outcome}`, `pgf_transaction_duration_seconds{datasource}` · `TRACE` span `pgf.transaction {isolation}` |
| Savepoint | `TRACE` span event `savepoint` — depth as an attribute |
| Retry (`run_transaction`) | `METRIC` `pgf_transaction_retries_total{datasource,reason}` (`serialization_failure` / `deadlock`), `pgf_transaction_attempts{datasource}` histogram · `LOG` `transaction.retried {attempt, reason, backoff_ms}` |
| Retry exhausted | `LOG` `transaction.retry_exhausted {attempts}` — distinct from a single failure; it means the retry budget is misconfigured or contention is real |
| `execute_many` | `METRIC` `pgf_batch_write_rows{datasource}` |
| `COPY` ingest | `METRIC` `pgf_copy_rows_total{datasource}`, `pgf_copy_duration_seconds{datasource}` |
| `LISTEN/NOTIFY` | `METRIC` `pgf_notify_events_total{datasource,channel}`, `pgf_listen_resubscribes_total{datasource,channel}` · `LOG` `listen.resubscribed` |
| Watermark fetch | `METRIC` `pgf_fetch_since_rows{datasource}` · `LOG` `fetch_since.page {rows, has_more}` — a page consistently at `limit` means the consumer is falling behind |

### L1 · Connection & resources

| Point | Signals |
|-------|---------|
| **Pool acquire wait** | `METRIC` `pgf_pool_acquire_seconds{datasource}` histogram — **the single most valuable missing signal.** Saturation shows up here as rising p99 long before it shows up as a timeout counter | 
| Pool occupancy | `METRIC` `pgf_pool_size{datasource,state}` gauge, state ∈ `idle`/`in_use`/`total` |
| Connection lifecycle | `METRIC` `pgf_pool_connections_created_total{datasource}`, `pgf_pool_connections_recycled_total{datasource,reason}` (`max_idle`/`max_lifetime`/`error`) |
| Pool timeout | already `pgf_pool_timeouts_total`; add `LOG` `pool.timeout {datasource, waited_ms, pool_size}` |
| Circuit breaker transition | `METRIC` `pgf_breaker_state{datasource}` gauge (0 closed / 1 half-open / 2 open), `pgf_breaker_transitions_total{datasource,from,to}`, `pgf_breaker_rejected_total{datasource}` · `LOG` `breaker.opened {failures, threshold}` / `breaker.closed` — **state transitions are events, and belong in logs as well as metrics**, because a breaker that flaps between open and closed is invisible in a gauge sampled every 15 s |
| Health check | `METRIC` `pgf_datasource_health{datasource}` gauge (1 healthy / 0.5 degraded / 0 down), `pgf_health_check_duration_seconds{datasource}` · `LOG` `datasource.health_changed {from, to}` — **on change only, never per poll** |
| Warm-up failure | `LOG` `pool.warmup_failed {datasource, error}` — closes [06 §6.9 #5](./06-concurrency-batch-parallel.md), where the current code silently swallows this |

### L0 · Config & lifecycle

| Point | Signals |
|-------|---------|
| Startup | `LOG` `service.startup {version, profile, datasources:[names], workers}` · `METRIC` `pgf_startup_duration_seconds` |
| Config loaded | `LOG` `config.loaded {profile, datasource_count}` — **names yes, DSNs never** (§11.8) |
| Config invalid | `LOG` `config.invalid {error}` then exit — fail fast, and the log line is the only artifact the operator gets |
| Unresolved `${env:}` / `${secret:}` | `LOG` `config.unresolved {reference}` — **the reference name, not the resolved value** |
| Shutdown | `LOG` `service.shutdown {inflight, drained_ms, forced}` · `METRIC` `pgf_shutdown_drain_seconds` |

## 11.4 Trace shape

The span tree a single batch request should produce:

```
http.request  {route, method, request_id}
├─ auth.check {action, result}
├─ admission.acquire {scope=global, waited_ms}
└─ pgf.batch {mode=parallel, items=3}
   ├─ pgf.batch.item {item_id=a, op=view, datasource=main}
   │  ├─ pgf.compile {op=view}
   │  ├─ pgf.pool.acquire {datasource=main, waited_ms}
   │  └─ pgf.db.execute {db.system=postgresql, rows}
   ├─ pgf.batch.item {item_id=b, op=function, datasource=analytics}
   │  └─ …                                    ← concurrent sibling, not nested
   └─ pgf.batch.item {item_id=c, op=interval, datasource=main}
```

Two notes that are easy to get wrong:

- In `parallel` mode the item spans are **siblings that overlap in time**. If they
  render as sequential, the fan-out is not actually running concurrently — the
  trace is the cheapest test of requirement 4 there is.
- A `transactional` batch has one `pgf.transaction` span wrapping all items, and
  the **whole subtree repeats on retry**. Retry attempts should be distinguishable
  by an `attempt` attribute rather than looking like duplicate work.

## 11.5 Log events

Structured events, not sentences. Field names are stable; the event name is the
primary key.

| Event | Level | Key fields |
|-------|-------|-----------|
| `service.startup` / `service.shutdown` | info | version, profile, datasources, drained_ms |
| `config.loaded` / `config.invalid` / `config.unresolved` | info / error | profile, datasource_count, error, reference |
| `datasource.health_changed` | warn | datasource, from, to |
| `breaker.opened` / `breaker.half_open` / `breaker.closed` | warn / info | datasource, failures, threshold |
| `pool.timeout` / `pool.warmup_failed` | warn | datasource, waited_ms, pool_size |
| `query.slow` | warn | datasource, op, object, elapsed_ms |
| `query.failed` | warn | datasource, op, type, sqlstate_class |
| `transaction.retried` / `transaction.retry_exhausted` | info / warn | datasource, attempt, reason, backoff_ms |
| `batch.completed` / `batch.item_timeout` / `batch.rolled_back` | info / warn | mode, items, ok, errors, elapsed_ms |
| `stream.aborted` / `stream.failed` | info / warn | datasource, op, rows_sent, reason |
| `admission.rejected` | warn | scope, datasource, waited_ms |
| `auth.denied` | warn | action, resource, subject |
| `security.unsafe_identifier` | warn | op, field |
| `request.invalid` / `request.failed` | info / warn | route, status, type |

## 11.6 Metric type summary

| Prefix | Type | Notes |
|--------|------|-------|
| `*_total` | counter | monotonic |
| `*_seconds` | histogram | never a gauge — percentiles are the point |
| `*_inflight`, `*_active`, `*_size`, `*_state`, `*_health`, `*_slots_inuse` | gauge | |
| `pgf_query_rows`, `pgf_batch_items`, `pgf_stream_rows_total` | histogram / counter | distribution matters more than the mean |

## 11.7 Correlation

One id threaded end to end, so a slow dashboard panel can be traced to a specific
statement on a specific connection:

```
X-Request-Id  (client-supplied or generated)
   └─ every log event field `request_id`
   └─ trace id / root span attribute
   └─ batch item field `item_id`  →  logged as "<request_id>/<item_id>"
   └─ PostgreSQL `application_name`  ←  set per connection or per statement
```

The last hop is the valuable one and the one usually skipped: with `request_id`
in `application_name`, a row in `pg_stat_activity` can be tied back to the HTTP
request that caused it. Format and whether it is set per-connection or
per-statement is a config choice, since it costs a round-trip if done per
statement.

## 11.8 Redaction — what must never be emitted

| Never | Instead |
|-------|---------|
| DSN, password, any resolved `${secret:}` value | The data-source **name**. `dsn` is already `SecretStr`; keep it out of logs, spans, error bodies and `/openapi.json` |
| Query parameter **values** | Parameter **names**, or the count |
| Row data | Row **counts** |
| Raw SQL text (from `/query`, `/execute`) | A stable `sql_fingerprint` hash. Governed calls log `op` + `object` instead, which is more useful anyway |
| The offending value in `security.unsafe_identifier` | The field name — logging the payload turns the log store into the injection surface |
| Auth tokens, `Authorization` headers, cookies | The `subject` from the `Principal` |
| Full exception tracebacks in HTTP responses | Traceback to logs, `request_id` to the client — already the current behavior |

The existing `test_dsn_is_secret_and_not_leaked_in_repr` test is the seed of this;
[10](./10-testing-and-delivery-plan.md) should grow a redaction suite asserting no
credential appears in any log, span, error payload or response.

## 11.9 Cardinality budget

Metric labels are a footgun; a single unbounded label can cost more than the
service it measures.

| Safe as a metric label | Why |
|------------------------|-----|
| `datasource` | Bounded by config, typically < 10 |
| `op` | Bounded by the operation registry, ~9 |
| `mode` | 3 |
| `status`, `outcome`, `result`, `reason`, `state`, `scope` | Small closed enums |
| `type` (error class) | Bounded by the error hierarchy |
| `route` | Bounded by the generated route table |
| `version` | Bounded by the mounted API versions, typically 1–2 |

| Never a metric label | Put it here instead |
|----------------------|---------------------|
| `request_id`, `item_id` | Log field / span attribute |
| Parameter values, row values | Nowhere |
| Raw SQL text | Log field as a fingerprint |
| `object` (view/function name) | **Span attribute and log field, not a label** — bounded by the database schema in principle, but that bound is outside our control |
| `channel` (LISTEN) | Label *only if* channels are enumerated in config; otherwise a log field |

Additional rules:

- **No per-row spans or span events.** A 10M-row stream would emit 10M events. Use
  counters, and at most one span event per fetch batch.
- **Traces are sampled** (`observability.trace_sample_ratio`), but errors and
  statements above `slow_query_ms` should be sampled at 100% — the interesting
  requests are exactly the rare ones.
- **Health is logged on change, polled as a gauge.** Logging every poll produces
  noise proportional to probe frequency and tells you nothing.

## 11.10 Config keys this implies

Added to the `observability` block in [05](./05-configuration.md) — no threshold
is hard-coded:

```yaml
observability:
  provider: none            # binds the external project's adapter; none = Null Objects
  metrics_enabled: false
  tracing_enabled: false
  slow_query_ms: 250        # threshold for the query.slow event
  trace_sample_ratio: 0.05  # errors and slow queries always sampled regardless
  log_level: info
  redact_sql: true          # log a fingerprint instead of raw SQL text
  application_name: true    # thread request_id into PostgreSQL application_name
```

## 11.11 Where the seams already are

Nothing below needs inventing — the attachment points exist in the ported core:

| Seam | Location | What attaches |
|------|----------|---------------|
| `MetricsPort` / `LoggerPort` / `TracerPort` | `core/ports.py` | The external project's adapters, bound at the composition root |
| `NoopMetrics` / `NoopLogger` / `NoopTracer` | `_internal/observability/noop.py` | The zero-cost default (Null Object) |
| `MeteredPool` / `MeteredConnection` | `_internal/observability/metered.py` | Where L1/L2 metrics already live; most new signals belong here |
| `PoolFactory.build` | `_internal/connection/registry.py` | Where the decorator stack is assembled — a `TracedConnection` decorator slots in beside `MeteredConnection` with no change to callers |
| `ClockPort` | `core/ports.py` | Injectable time, so duration assertions in tests are deterministic |
| Middleware chain | `pgfoundation_api/middleware/` — `timing.py`, `correlation.py`, `admission.py` ([09](./09-project-structure.md)) | Request-level signals; one file per concern, so each can be enabled and tested on its own |
| Observability binding | `pgfoundation_api/bootstrap/providers.py` | The one place that knows the concrete provider — where the external project's adapters are registered |

## 11.12 Explicitly not built here

Per ADR-014, and repeated because it bounds the work: **no exporters, no OTLP or
collector configuration, no dashboards, no alert rules, no log storage or
retention.** Those belong to the external observability project. The deliverable
here is the emission points, the names, the fields, and the redaction and
cardinality rules — a contract that project can consume.
