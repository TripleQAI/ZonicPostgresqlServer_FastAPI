# pgfoundation (FastAPI) — Design Plan

The design plan for rebuilding the PostgreSQL Database Foundation Server
(`ZonicPostgresqlServer_DjangoApp`) on **FastAPI**, with the same features and a
sharper answer to four requirements:

| # | Requirement | Where it is answered |
|---|-------------|----------------------|
| 1 | **Same features & functionality** | [01 — Scope & Parity](./01-scope-and-parity.md) — a line-by-line parity matrix; nothing is dropped, several *designed-but-unbuilt* Django features are finally delivered. |
| 2 | **Low code, no hard-coding** | [04 — Declarative Operation Registry](./04-declarative-operation-registry.md) — one table of operation descriptors drives the unary REST surface, the batch dispatcher, the streaming surface and OpenAPI. Adding a capability = adding a row. [05 — Configuration](./05-configuration.md) — every operational value is externalized and validated. |
| 3 | **Proper design patterns** | [03 — Design Patterns](./03-design-patterns.md) — the inherited catalog (Ports & Adapters, Facade, Registry, UoW, Strategy, Decorator, Null Object…) plus the patterns the new concurrency work needs (Command, Scatter–Gather, Bulkhead, Semaphore-bounded backpressure). |
| 4 | **High performance, throughput, concurrency, parallel & batch calls** | [06 — Concurrency, Batch & Parallel](./06-concurrency-batch-parallel.md) and [07 — Performance](./07-performance.md) — the batch/parallel API, bounded fan-out, psycopg pipeline mode, prepared statements, `COPY`, streaming responses, and the concurrency-correctness defects to fix while porting. |

> **Status: P0–P4 built, P5 in progress.** Documents 01–13 describe the intended
> design; [14 — Build Status](./14-build-status.md) records what is actually
> implemented, the corrections made while building, and the known gaps.

> 🌐 **Open in a browser:** [`pgfoundation-fastapi-design.html`](./pgfoundation-fastapi-design.html)
> — the whole design, rendered: sidebar navigation, rendered Mermaid diagrams,
> light/dark theme, and print-to-PDF.
>
> 📄 **Everything in one Markdown file:** [`pgfoundation-fastapi-design.md`](./pgfoundation-fastapi-design.md)
> — all fourteen documents and all eight ADRs combined, with the cross-references
> rewritten as internal links.
>
> The split files below are the same content, and are the source the two combined
> files are built from.

---

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

## Documents

| Doc | Contents |
|-----|----------|
| [01 — Scope & Parity](./01-scope-and-parity.md) | What exists today, feature-by-feature parity matrix, what is added, what stays out of scope. |
| [02 — Architecture](./02-architecture.md) | Layers, ports & adapters, dependency rule, the C4-ish views, what changes vs Django. |
| [03 — Design Patterns](./03-design-patterns.md) | Pattern catalog with the problem each one solves, and pattern → requirement traceability. |
| [04 — Declarative Operation Registry](./04-declarative-operation-registry.md) | **The low-code mechanism.** One descriptor table → routes + batch dispatch + streaming + OpenAPI. |
| [05 — Configuration](./05-configuration.md) | The config model, the provider chain, and the "no hard-coded value" audit. |
| [06 — Concurrency, Batch & Parallel](./06-concurrency-batch-parallel.md) | Batch API, execution strategies, bounded fan-out, backpressure, timeouts, cancellation, correctness fixes. |
| [07 — Performance](./07-performance.md) | Round-trip reduction, serialization, row formats, pooling, prepared statements, budgets. |
| [08 — API Surface](./08-api-surface.md) | Every endpoint, request/response shape, error mapping. |
| [09 — Project Structure](./09-project-structure.md) | Directory layout, packaging, architecture contracts. |
| [10 — Testing & Delivery](./10-testing-and-delivery-plan.md) | Test strategy, parity harness, phased delivery plan. |
| [11 — Observability: Instrumentation Points](./11-observability-instrumentation-points.md) | **Comments only, no code.** Every point that must emit a metric, span or log event; the signal vocabulary; redaction and cardinality rules. |
| [12 — Deployment Targets & Environments](./12-deployment-targets.md) | The Timescale Cloud (AWS) target, DSN handling, TLS, connection budget, least-privilege roles, deployment checklist. |
| [13 — Implementation Plan, Phase by Phase](./13-implementation-plan.md) | **Start here to build.** The six phases as ~60 tasks with dependencies, sizing, exit criteria and a risk register. |
| [14 — Build Status](./14-build-status.md) | **What is actually built.** Phase status, corrections made while building, known gaps. |
| [15 — CI/CD and AWS Deployment Plan](./15-cicd-and-deployment.md) | **How code reaches AWS.** Branch→CI→ECR→ECS Fargate pipeline, the connection-budget ceiling that rules out serverless, and the prod-profile prerequisites. |

## Decisions (ADRs)

| ADR | Decision |
|-----|----------|
| [ADR-001](./adr/ADR-001-fastapi-service-shell.md) | Service shell is **FastAPI** (reinstating the original ADR-002 direction, superseding Django ADR-012). |
| [ADR-002](./adr/ADR-002-core-ported-unchanged.md) | The core engine is **ported unchanged**; only the shell is rewritten. |
| [ADR-003](./adr/ADR-003-declarative-operation-registry.md) | Endpoints are **generated from a descriptor table**, not hand-written per operation. |
| [ADR-004](./adr/ADR-004-batch-and-parallel-execution.md) | Batch is **one endpoint with pluggable execution strategies**, not one endpoint per mode. |
| [ADR-005](./adr/ADR-005-di-via-lifespan-and-depends.md) | Wiring uses **lifespan + `app.state` + `Depends`**; module-global singletons are removed. |
| [ADR-006](./adr/ADR-006-streaming-delivery.md) | Streaming delivery is **NDJSON + SSE over `StreamingResponse`**, content-negotiated. |
| [ADR-007](./adr/ADR-007-serialization-and-row-format.md) | **orjson** with an explicit `Decimal → string` rule, and a selectable `row_format`. |
| [ADR-008](./adr/ADR-008-auth-boundary.md) | **Caller** authN + coarse datasource×action authZ belong here; **end-user** authN/authZ stays with the higher-level APIs. Amends the inherited ADR-013. |
