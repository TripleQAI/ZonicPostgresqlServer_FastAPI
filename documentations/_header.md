# pgfoundation on FastAPI — Complete Design Plan

**A design-only plan** for rebuilding the PostgreSQL Database Foundation Server
(`ZonicPostgresqlServer_DjangoApp`) on **FastAPI**.

> **Status: P0-P4 built, P5 in progress.** This plan is no longer design-only —
> see [Part I · 14](#doc-14) for what is implemented, what was corrected while
> building, and what is still open. Sections 01-13 describe the intended design;
> where the build diverged, 14 records why.
>
> *This file is generated. The source is the split files in this directory
> (`01-…md` … `11-…md`, `adr/ADR-00N-…md`); rebuild with `python build.py`.*

> **Instrumentation markers.** Code sketches carry `# OBSERVE (METRIC|TRACE|LOG):`
> comments marking where telemetry belongs — comments only, deliberately not
> implemented. [Part I · 11](#doc-11) is the full catalogue: the signal
> vocabulary, redaction rules and cardinality budget.

---

<a id="the-four-requirements"></a>

## The four requirements

| # | Requirement | Where it is answered |
|---|-------------|----------------------|
| 1 | **Same features & functionality** | [Part I · 01](#doc-01) — a line-by-line parity matrix; nothing is dropped, and several *designed-but-unbuilt* Django features are finally delivered. [ADR-002](#adr-002) — the core engine is ported, not reimplemented, so parity is structural rather than re-derived. |
| 2 | **Low code, no hard-coding** | [Part I · 04](#doc-04) — one table of operation descriptors drives the unary REST surface, the batch dispatcher, the streaming surface and OpenAPI. Adding a capability = adding a row. [Part I · 05](#doc-05) — every operational value is externalized and validated. |
| 3 | **Proper design patterns** | [Part I · 03](#doc-03) — the inherited catalog (Ports & Adapters, Facade, Registry, Unit of Work, Strategy, Decorator, Null Object…) plus the patterns the new concurrency work needs (Command, Scatter–Gather, Bulkhead, semaphore-bounded backpressure). Two anti-patterns present in the current code are removed. |
| 4 | **High performance, throughput, concurrency, parallel & batch calls** | [Part I · 06](#doc-06) and [Part I · 07](#doc-07) — the batch/parallel API, bounded fan-out, admission control, psycopg pipeline mode, prepared statements, `COPY`, streaming responses, and nine concurrency defects found in the current implementation. |

---

<a id="the-one-paragraph-version"></a>

## The one-paragraph version

The Django project already has the right shape: a **framework-free core engine**
(`pgfoundation`) behind ports & adapters, wrapped by a **thin transport shell**
(`pgfoundation_service`). That split is the reason this port is cheap — *the core
is kept essentially as-is* and only the shell is rewritten. The shell rewrite is
where all four requirements are won: FastAPI removes the hand-written OpenAPI
builder and hand-written routing table, a **declarative operation registry**
removes the hand-written per-endpoint view functions, `Depends` + `lifespan`
removes the global-singleton service locator, and a new **batch/parallel
execution layer** turns "one HTTP call = one query" into "one HTTP call = N
queries, executed with a chosen concurrency strategy against N pools."

---

<a id="contents"></a>

## Contents

**Part I — Design**

| § | Document | Contents |
|---|----------|----------|
| [01](#doc-01) | **Scope & Feature Parity** | What exists today, the feature-by-feature parity matrix, what is added, what stays out of scope. |
| [02](#doc-02) | **Architecture** | Layers, ports & adapters, the dependency rule, request path, what changes vs Django. |
| [03](#doc-03) | **Design Patterns** | Pattern catalog with the problem each solves, and pattern → requirement traceability. |
| [04](#doc-04) | **The Declarative Operation Registry** | **The low-code mechanism.** One descriptor table → routes + batch dispatch + streaming + OpenAPI. |
| [05](#doc-05) | **Configuration** | The config model, the provider chain, and the "no hard-coded value" audit. |
| [06](#doc-06) | **Concurrency, Batch & Parallel** | Batch API, execution strategies, bounded fan-out, backpressure, timeouts, cancellation, correctness fixes. |
| [07](#doc-07) | **Performance** | Round-trip reduction, serialization, row formats, pooling, prepared statements, budgets. |
| [08](#doc-08) | **API Surface** | Every endpoint, request/response shape, frozen contracts, error mapping. |
| [09](#doc-09) | **Project Structure** | Directory layout, packaging, architecture contracts. |
| [10](#doc-10) | **Testing & Delivery** | Test strategy, parity harness, six-phase delivery plan. |
| [11](#doc-11) | **Observability: Instrumentation Points** | **Comments only, no code.** Every point that must emit a metric, span or log event; the signal vocabulary; redaction and cardinality rules. |
| [12](#doc-12) | **Deployment Targets & Environments** | The Timescale Cloud (AWS) target, DSN handling, TLS, the connection budget, least-privilege roles, and the deployment checklist. |
| [13](#doc-13) | **Implementation Plan, Phase by Phase** | The six phases expanded to ~60 tasks: what each touches, what it depends on, what proves it done, and where the plan can silently go wrong. |
| [14](#doc-14) | **Build Status** | What is actually implemented, corrections made while building, and the known gaps. |

**Part II — Decisions (ADRs)**

| ADR | Decision |
|-----|----------|
| [ADR-001](#adr-001) | Service shell is **FastAPI** (reinstating the original ADR-002 direction, superseding Django ADR-012). |
| [ADR-002](#adr-002) | The core engine is **ported unchanged**; only the shell is rewritten. |
| [ADR-003](#adr-003) | Endpoints are **generated from a descriptor table**, not hand-written per operation. |
| [ADR-004](#adr-004) | Batch is **one endpoint with pluggable execution strategies**, not one endpoint per mode. |
| [ADR-005](#adr-005) | Wiring uses **lifespan + `app.state` + `Depends`**; module-global singletons are removed. |
| [ADR-006](#adr-006) | Streaming delivery is **NDJSON + SSE over `StreamingResponse`**, content-negotiated. |
| [ADR-007](#adr-007) | **orjson** with an explicit `Decimal → string` rule, and a selectable `row_format`. |
| [ADR-008](#adr-008) | **Caller** authN + coarse datasource×action authZ belong here; **end-user** authN/authZ stays with the higher-level APIs. Amends the inherited ADR-013. |

---

<a id="delivery-at-a-glance"></a>

## Delivery at a glance

| Phase | Delivers | Exit |
|-------|----------|------|
| **P0** | Port the core engine | Ported core tests pass; architecture contracts green |
| **P1** | Registry-driven FastAPI shell at feature parity | Parity harness green on all five existing endpoints; real Swagger UI |
| **P2** | Batch & parallel execution | Concurrency tests green; parallel batch beats sequential under load |
| **P3** | Streaming (NDJSON/SSE), interval, fetch-since, copy, execute-many | Flat memory over a 10M-row stream; disconnect releases the connection |
| **P4** | Concurrency hardening | Chaos suite green; no leaks in a soak run |
| **P5** | Performance & operations | Performance budgets ([07](#doc-07) §7.9) met and CI-gated |

---
