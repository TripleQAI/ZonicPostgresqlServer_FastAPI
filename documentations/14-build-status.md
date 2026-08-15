# 14 — Build Status

What is actually implemented, as against the plan in
[13](./13-implementation-plan.md). Updated as phases land.

**Last updated:** 2026-08-14 · **Suite:** 225 passed, 4 skipped (live PostgreSQL)
· ruff clean · mypy clean · 4 architecture contracts kept.

## 14.1 Phase status

| Phase | Status | Notes |
|-------|--------|-------|
| **P0** — port the core | ✅ **complete** | `diff -r` against the Django repo reports **IDENTICAL**. The ADR-002 guarantee holds by construction. |
| **P1** — registry shell at parity | ✅ **complete** | The P1.9 spike passed; the parity harness agrees on every operation. |
| **P2** — batch & parallel | ✅ **complete** | Three strategies, admission control, pipeline seam. P2.14 (load comparison) deferred to P5 with the load harness. |
| **P3** — streaming | ✅ **complete** | NDJSON + SSE, subscribe, and the four operations the Django docs designed but never built. P3.11 (live integration) blocked — see §14.4. |
| **P4** — hardening & security | 🟡 **mostly** | 8 of 9 concurrency defects closed; caller auth + policy shipped. P4.10/P4.11 are deployment artifacts. |
| **P5** — performance & ops | ✅ **complete** | `row_format`, prepared-statement tuning, per-source `itersize`, the metrics adapter, `/metrics`, the request/batch/admission/auth signals, and the benchmark harness with CI gates. Measured: **parallel batch 10.5×**, shell overhead **1%** of a realistic request ([07 §7.9](./07-performance.md)). |

## 14.2 What the code delivers today

**The operations table is 9 rows** — `view`, `function`, `procedure`, `query`,
`execute`, `interval`, `fetch_since`, `execute_many`, `copy` — and each row
produces a unary endpoint, an OpenAPI operation, a batch-dispatch entry, and
(when `streamable`) a streaming endpoint. That is [ADR-003](./adr/ADR-003-declarative-operation-registry.md)
holding up under its first real extension: the four P3 operations each cost one
row plus a DTO.

| Surface | State |
|---------|-------|
| Governed invocation (`view`/`function`/`procedure`) | ✅ at parity, injection boundary intact |
| Raw SQL (`query`/`execute`) | ✅ gated by `api.enable_raw_sql`, **not registered** when off |
| `interval`, `fetch-since`, `execute-many`, `copy` | ✅ new — designed in the Django docs, never built there |
| `POST /v1/batch` — parallel · sequential · transactional | ✅ new |
| `/stream` (NDJSON + SSE), `/subscribe/{channel}` | ✅ new |
| `/health`, `/ready` unversioned + `/v1` aliases | ✅ tri-state health |
| OpenAPI + Swagger UI + ReDoc | ✅ generated |
| Caller auth + coarse policy ([ADR-008](./adr/ADR-008-auth-boundary.md)) | ✅ mesh-identity and shared-secret providers shipped |
| `row_format: dict \| tuple` ([ADR-007](./adr/ADR-007-serialization-and-row-format.md)) | ✅ new — driver-level row factory, not post-processing |
| `/metrics` + a `MetricsPort` adapter ([11](./11-observability-instrumentation-points.md)) | ✅ new — Prometheus text, Null Object by default, cardinality enforced in code |

## 14.3 Corrections made while building

Recorded here rather than quietly implemented differently from the design.

| Doc | Correction |
|-----|-----------|
| [ADR-007](./adr/ADR-007-serialization-and-row-format.md) | `ORJSONResponse` is deprecated; FastAPI's native Pydantic serialization is faster **and** preserves the `Decimal → "42.00"` contract. Both paths were measured before choosing. The orjson encoder is retained for NDJSON/SSE, which bypass Pydantic. |
| [06 §6.9](./06-concurrency-batch-parallel.md) item 1 | The circuit-breaker **counter** was never racy — those methods contain no `await`. The real defect was unbounded probes in `HALF_OPEN`, which spans one. |
| [ADR-003](./adr/ADR-003-declarative-operation-registry.md) | Added the `invoke` override for operations whose handle call is not `method(one_argument)`, and consolidated on `spec.call()`. |
| [09 §9.1](./09-project-structure.md) | `registry/mappers.py` lives at `api/v1/mappers.py` — mappers are version-specific, the engine is not. `security/providers.py` was added by P4.8. |
| [07 §7.5](./07-performance.md) | `row_format` is bound **on the handle** (`foundation.datasource(name, row_format=…)`) rather than passed per call, so adding it did not change nine operation signatures. The driver-level row factory — the part that matters — is as designed. |
| [11 §11.3](./11-observability-instrumentation-points.md) | `pgf_http_requests_inflight` is **unlabelled**: middleware runs before routing, so the route template is not resolvable on the way in, and the concrete path would mint a series per data source. |
| [08 §8.2](./08-api-surface.md) | `columns` is **absent**, not `null`, in dict format. Emitting `"columns": null` on every read would be a visible wire change for a feature the caller never opted into, so the generated routes serialize with `exclude_none`. |
| [07 §7.2](./07-performance.md) | Validated identifiers are now **quoted** on emission (`"schema"."table"`), not interpolated bare. See §14.6 — without this, the governed API could not name a mixed-case relation at all. |

Two findings the type checker produced that the design had also reached by
reading the code — `ConnectionPort` missing `copy_stream`/`listen`, and
`MeteredPool.release` narrowing its parameter — are closed (P2.11, P4.2).

A third instance of that second defect was found later, in the driver itself:
`PsycopgPool.release` also narrowed its parameter to `PsycopgConnection`. Because
that made `PsycopgPool` fail to satisfy `PoolPort`, `PsycopgDriver` in turn
failed `DriverPort` — so the *concrete adapter the service actually runs* did not
typecheck against the seam it exists to implement. Widened to `ConnectionPort`
with an explicit runtime check.

## 14.4 Known gaps

| Gap | Why it is open |
|-----|----------------|
| **P3.11 live integration tests** | Needs only a password now. The pooling question is **resolved**: the console shows no connection pooler, so `32064` is the direct session endpoint and cursors / `LISTEN/NOTIFY` / session `SET` all work ([12 §12.6](./12-deployment-targets.md)). TLS `verify-full` is verified against the system trust store. |
| **P4.7 partial** | The pinned-connection isolation invariant is asserted in `UnitOfWork`; the live isolation-level test belongs with P3.11. |
| **P4.10/P4.11** | Prod-profile assertions exist in `config/profiles/prod.yaml`; the least-privilege database role and `sslmode=verify-full` are deployment actions against the real instance. |
| **Trace and log emission** | The metric points are implemented and gated. The remaining `OBSERVE` markers are `TRACE` and `LOG` points, which need a real `TracerPort` / `LoggerPort` adapter to bind to — per ADR-014 that adapter belongs to the external observability project. |
| **Soak test (P5.9)** | Hours-long leak detection needs a live database and a scheduler; belongs with P3.11. |
| **Public-surface strict typing** | Deliberately deferred: enabling `disallow_untyped_defs` surfaces two ported untyped defs, so the flip is scheduled with their annotations. The override sits commented in `pyproject.toml`. |

## 14.5 Operational constraint

**Do not onboard a second consuming API until the deployment carries real caller
identity.** The providers are built and tested, but a deployment still has to
choose one, supply credentials from its secret manager, and populate
`auth.policy.callers`. Until then the service is safe only while the trusted
network contains exactly one caller ([ADR-008](./adr/ADR-008-auth-boundary.md)).

## 14.6 Mixed-case identifiers — a functional gap the port inherited

Found against the deployed database, not in a test.

The compilers validated an identifier and then interpolated it **verbatim**.
PostgreSQL folds an *unquoted* identifier to lower case, so a relation created
as `"agentBuilder_agentworkflow"` — which is what Django and most ORMs emit —
compiled to `SELECT * FROM public.agentBuilder_agentworkflow`, which the server
resolved as `public.agentbuilder_agentworkflow` and rejected with *relation does
not exist*.

The consequence was larger than a failed query. **Every camelCase table in the
target schema was unreachable through the name-based surface**, so the only way
to read them was `/query` with hand-quoted raw SQL — precisely the endpoint the
design keeps off the production surface ([08 §8.3](./08-api-surface.md)). A
governed API that cannot name half the objects in the database pushes callers to
the ungoverned one.

`quote_identifier()` in `_internal/access/sql.py` now validates *and* quotes;
`compile_view` / `compile_function` / `compile_procedure` / `compile_interval` /
`build_keyset_query` / `copy_stream` / `listen` all emit through it.

Three properties make this safe rather than merely convenient:

* **It cannot widen the injection surface.** `_IDENT` already excludes the quote
  character, so an emitted identifier can never terminate its own quoting. The
  validation gate is unchanged; only the rendering is.
* **It is behaviour-preserving for lower-case names.** `"orders"` and `orders`
  denote the same relation, so no existing caller changes.
* **Quoting is applied to the identifier only.** In `f(arg => %(arg)s)` the same
  string is both a SQL argument name and a psycopg placeholder key; the
  identifier is quoted and the key stays bare, or binding breaks.

Verified against the live service: the mixed-case relation now resolves through
`/view` with columns, filters and ordering; lower-case relations are unchanged;
injection attempts are still refused at validation.
