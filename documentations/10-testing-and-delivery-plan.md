# 10 — Testing & Delivery Plan

## 10.1 What the current suite covers

69 tests: 65 pass without a database (in-memory `FakeDriver`), 4 live-PostgreSQL
tests skip unless `PGF_TEST_DSN` is set. Plus 4 import-linter contracts.

| Area | Tests |
|------|-------|
| Config validation, secret redaction, env interpolation, fail-fast | 7 |
| Facade walking skeleton, handle, lifecycle, health, unknown source | 6 |
| Invocation compilers (view/function/procedure), injection rejection, determinism | 14 |
| Circuit breaker state machine, resilient pool, metering | 6 |
| Streaming, interval compilation, keyset/watermark, `COPY` | 12 |
| Transactions, savepoints, retry, `execute_many`, repository | 8 |
| Service API, error mapping, OpenAPI, auth enabled/disabled/fail-closed | 14 |
| Live smoke (transactions, `execute_many`, streaming, interval, watermark) | 4 |

**All of these port.** The core tests move unchanged. The 14 service tests are
rewritten against `httpx.AsyncClient` instead of Django's `AsyncRequestFactory` —
same assertions, different client.

## 10.2 Test pyramid

| Level | Scope | DB? |
|-------|-------|-----|
| **Unit** | Compilers, UoW, retry, breaker, config chain, batch strategies against `FakeDriver` | no |
| **Service** | The full app over ASGI with a fake-driver foundation — routes, validation, error mapping, auth, batch envelopes | no |
| **Contract** | Library-mode ↔ service-mode parity; OpenAPI snapshot; registry invariants | no |
| **Integration** | Real psycopg against real PostgreSQL — pooling, transactions, isolation, cursors, `COPY`, pipeline mode, SQLSTATE mapping | yes |
| **Load / soak** | Throughput, latency SLOs, saturation behavior, leak detection | yes |

## 10.3 New tests requirement 2 needs

The registry invariant is only real if a test enforces it ([04 §4.4](./04-declarative-operation-registry.md)):

- every `OperationSpec` has a route;
- every `OperationSpec` is batch-dispatchable;
- every `OperationSpec` appears in `/openapi.json` with its request schema;
- every `streamable` spec has a `/stream` route that emits NDJSON;
- no data-plane route exists outside the registry (admin/docs on an allow-list);
- `enable_raw_sql: false` removes the raw-SQL routes from both the app **and** the
  OpenAPI document.

## 10.4 New tests requirement 1 needs — the parity harness

The single most valuable test in this port. For each operation, run the *same*
logical call through both paths and assert identical results:

```python
@pytest.mark.parametrize("spec", OPERATIONS, ids=lambda s: s.op)
async def test_library_and_service_agree(spec, foundation, client, sample_payload):
    lib = await getattr(foundation.datasource("main"), spec.handle_method)(
        spec.to_core(spec.request_model.model_validate(sample_payload[spec.op])))
    api = await client.post(f"/v1/datasources/main/{spec.suffix}",
                            json=sample_payload[spec.op])
    assert api.json()["rows"] == jsonable(lib.rows)
```

Plus the frozen behavioral contracts from [08 §8.2](./08-api-surface.md):

| Test | Guards |
|------|--------|
| `test_numeric_serializes_as_string` | `Decimal("42.00")` → `"42.00"`, not `42.0`. The orjson switch is exactly where this breaks. |
| `test_injection_in_object_name_rejected` | `"v_paid_orders; DROP TABLE x"` → `400` |
| `test_response_envelope_field_names` | `rows` / `row_count` / `elapsed_ms` unchanged |
| `test_schema_alias_accepted` | request field `schema` still works |
| `test_error_body_leaks_nothing` | no SQL text, DSN, or traceback in any error body |
| `test_openapi_snapshot` | diff against a committed snapshot; changes must be intentional |
| `test_compilation_is_deterministic_for_same_shape` | ported — protects prepared-statement reuse |

## 10.5 New tests requirement 4 needs

| Test | Asserts |
|------|---------|
| `test_parallel_batch_runs_concurrently` | With a fake driver that sleeps 50 ms per item, 10 items complete in ~50–100 ms, not ~500 ms |
| `test_parallel_batch_respects_max_concurrency` | Peak observed in-flight count never exceeds the configured cap |
| `test_batch_partial_failure_does_not_cancel_siblings` | One failing item ⇒ 200, one `error`, the rest `ok` |
| `test_transactional_batch_rolls_back_all_on_failure` | Fake driver records 1 begin, 0 commits, 1 rollback |
| `test_transactional_batch_retries_on_transient` | Transient on attempt 1, success on 2 ⇒ one result, two attempts |
| `test_sequential_batch_uses_one_connection` | `acquired == 1` for an N-item batch |
| `test_batch_exceeding_max_items_rejected` | `422` before any database work |
| `test_admission_timeout_returns_503_fast` | Over-capacity request rejected in < 5 ms, never a pool timeout |
| `test_stream_releases_connection_on_client_disconnect` | Pool `released == acquired` after an aborted stream |
| `test_stream_memory_is_flat` | RSS stays within a band while streaming a large fake result |
| `test_streams_are_not_batch_dispatchable` | A `streamable` op inside a batch item is rejected ([06 §6.5](./06-concurrency-batch-parallel.md)) |
| `test_health_gathers_in_parallel` | 5 sources × 100 ms ⇒ ~100 ms, not ~500 ms |
| `test_batch_concurrency_peak_metric` | The recorded peak approaches the configured cap — the metric that proves the fan-out ([11 §11.3](./11-observability-instrumentation-points.md)) |
| `test_no_secret_appears_in_any_signal` | Recording `MetricsPort`/`LoggerPort` spies see no DSN, password, parameter value or row value ([11 §11.8](./11-observability-instrumentation-points.md)) |
| `test_metric_labels_are_bounded` | Every emitted label value comes from a closed set — no `request_id`, SQL text, or object name as a metric label ([11 §11.9](./11-observability-instrumentation-points.md)) |
| `test_circuit_breaker_under_concurrent_failures` | 50 concurrent failures with threshold 5 ⇒ opens exactly once; one probe in half-open |
| `test_unique_cursor_names_under_concurrency` | Two concurrent streams never collide |

## 10.6 Chaos / resilience (ported, extended)

| Scenario | Expectation |
|----------|-------------|
| Kill the database mid-test | Breaker opens, `/v1/ready` flips to 503, calls fast-fail; recovers when the DB returns |
| Inject transient errors | Retry succeeds within `max_attempts`; non-idempotent writes are not retried |
| Saturate the pool | `PoolTimeoutError` → 503, never a hang; metrics record it |
| Saturate the **limiter** (new) | Fast 503 + `Retry-After` *before* pool acquire |
| Slow query | `statement_timeout` fires; connection returns to the pool clean |
| Client disconnects mid-stream (new) | Query cancelled server-side; connection released |
| SIGTERM under load | In-flight work drains within the deadline; no dropped commits |

## 10.7 CI gates

1. Ruff lint + format
2. mypy/pyright strict on the public surface
3. **import-linter** contracts (7 — including shell-uses-public-surface-only and the two intra-shell layering contracts, [09 §9.3](./09-project-structure.md))
4. Unit + service + contract suites green
5. Integration suite green when `PGF_TEST_DSN` is set
6. Parity harness green
7. Registry-invariant tests green
8. Perf micro-benchmark within budget
9. `pip-audit` / `bandit` clean

## 10.8 Delivery phases

Each phase is independently shippable and verifiable.

```mermaid
graph LR
    P0[P0 · Port the core] --> P1[P1 · Registry shell at parity]
    P1 --> P2[P2 · Batch & parallel]
    P2 --> P3[P3 · Streaming]
    P3 --> P4[P4 · Concurrency hardening]
    P4 --> P5[P5 · Perf & ops]
```

### P0 — Port the core (lowest risk, highest confidence)
Copy `pgfoundation` verbatim; port all core tests; wire import-linter.
**Exit:** every ported core test passes, contracts green. No shell yet.

### P1 — Registry-driven shell at feature parity
`app.py`, `settings.py`, `errors.py`, `ops.py`, `bootstrap/`, `registry/`,
`security/`, and `api/v1/` (operations table, mappers, schemas, `routes/admin.py`).
Only the five Django endpoints, generated from the table.
**Exit:** the parity harness passes for all five; the frozen contracts of
[08 §8.2](./08-api-surface.md) hold; `/docs` serves real Swagger UI; the
registry-invariant tests pass. **This is the "same features" milestone.**

### P2 — Batch & parallel (requirement 4, part 1)
`batch/` package: models, dispatch, three strategies, limits. Core-side fixes for
parallel `health`/`start`/`aclose`. Admission control middleware.
**Exit:** the §10.5 concurrency tests pass; a 20-item parallel batch beats
sequential by roughly the expected factor under load test.

### P3 — Streaming (delivers the designed-but-unbuilt features)
NDJSON + SSE `StreamingResponse`; `/stream` variants; `subscribe`; `copy`;
`interval`; `fetch-since`; `execute-many` — most of which are registry rows once
the streaming adapter exists.
**Exit:** flat memory over a 10M-row stream; disconnect releases the connection;
live integration tests green.

### P4 — Concurrency hardening
The [06 §6.9](./06-concurrency-batch-parallel.md) defect list: breaker locking,
cursor naming, decorator unwrap contract, warm-up error visibility, timeout and
cancellation propagation to `statement_timeout`, graceful drain.
**Exit:** chaos suite green; no leaks in a soak run.

### P5 — Performance & operations
orjson + `Decimal` rule, `row_format`, prepared-statement tuning, pipeline mode
tuning, `max_response_rows`, profiles, `/metrics` seam, load-test SLO validation.
**Also:** work through the `OBSERVE` markers from
[11](./11-observability-instrumentation-points.md) — `grep -rn "OBSERVE"` is the
work list — and bind the external observability project's adapters at the
composition root.
**Exit:** the [07 §7.9](./07-performance.md) budgets met and CI-gated; no `OBSERVE`
marker left unimplemented or unwaived; the redaction and cardinality suites green.

## 10.9 Definition of done

- Every row of the [01 §1.3](./01-scope-and-parity.md) parity matrix is delivered
  or explicitly deferred with a reason.
- Parity harness green: library mode and service mode agree on every operation.
- Registry invariants enforced by tests — adding a capability touches one row.
- Zero hard-coded operational values; the [04 §4.7](./04-declarative-operation-registry.md)
  audit list is closed.
- Batch/parallel/streaming SLOs met and CI-gated.
- The [06 §6.9](./06-concurrency-batch-parallel.md) concurrency defects closed.
- Every `OBSERVE` marker from [11](./11-observability-instrumentation-points.md)
  either implemented or explicitly waived; redaction and cardinality rules tested.
- Architecture contracts green, including shell-uses-public-surface-only.
- Prod profile asserts `auth.enabled: true`, `enable_raw_sql: false`,
  `docs_enabled: false`.
