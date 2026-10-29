# ZonicPostgresqlServer_FastAPI


A rebuild of the PostgreSQL Database Foundation Server (`pgfoundation`) on
**FastAPI**, ported from `ZonicPostgresqlServer_DjangoApp` (the earlier Django
version, not public).

> **Status: implemented, being deployed.** Phases P0–P5 are mostly complete —
> 225 tests passing, ruff/mypy clean — see
> [`documentations/14-build-status.md`](documentations/14-build-status.md) for the
> exact phase-by-phase state. CI/CD is now configured (`Dockerfile`, `Jenkinsfile`,
> the `deploy/helm/zonic-postgresql-server` chart) per
> [`documentations/15-cicd-and-deployment.md`](documentations/15-cicd-and-deployment.md)
> and the infra repo's `docs/TripleQAI-enterprise-aws-cicd-deployment-plan.md` §12;
> first deploy to the development EKS environment is in progress. The service
> currently runs on AWS Lambda; see [Current deployment](#current-deployment).
>
> 🌐 **Read the original design plan in a browser:** [`documentations/pgfoundation-fastapi-design.html`](documentations/pgfoundation-fastapi-design.html)
> — the whole plan rendered, with navigation, diagrams and print-to-PDF.
>
> 📄 **Or as one Markdown file:** [`documentations/pgfoundation-fastapi-design.md`](documentations/pgfoundation-fastapi-design.md)
>
> Prefer it split up? The same content is in [`documentations/`](documentations/README.md)
> as fifteen documents plus eight ADRs.

---

## Nebius x NVIDIA Global AI Hackathon

This repository is part of the Zonic agent platform, our entry in the Nebius x NVIDIA Global AI Hackathon (Best Apps and Agents track). The platform's agents run on NVIDIA Nemotron models served by Nebius Token Factory. How that is wired, and which model does what, is in the runtime's README: [NVIDIA Nemotron on Nebius Token Factory](https://github.com/TripleQAI/ZonicAgentRuntimeBackend_Python_LangGraph#nvidia-nemotron-on-nebius-token-factory).

**This repository's part:** Every service stores its data through this gateway in Neon PostgreSQL: the builder's workflows and the runtime's runs, sessions and traces, which include each Nemotron call's token usage.

**Live demo:** the chat at https://dev.tripleq.ai/agent-runtime-lg and the agent builder at https://dev.tripleq.ai/agent-visual-builder-lg/workflows. Both sign in with Amazon Cognito; the test accounts are in the hackathon submission's testing instructions.

| Repository | Role |
|---|---|
| [ZonicAgentBuilderUI_NextJS](https://github.com/TripleQAI/ZonicAgentBuilderUI_NextJS) | The visual builder: design a workflow and pick a Nemotron model for each node |
| [ZonicAgentBuilderServer_FastAPI](https://github.com/TripleQAI/ZonicAgentBuilderServer_FastAPI) | The builder's API: validation, import and export, the node registry and model options |
| [ZonicAgentRuntimeBackend_Python_LangGraph](https://github.com/TripleQAI/ZonicAgentRuntimeBackend_Python_LangGraph) | Runs the workflows on LangGraph and calls Nemotron through Token Factory |
| [ZonicAgentRuntimeUI_NextJS_For_LangGraph](https://github.com/TripleQAI/ZonicAgentRuntimeUI_NextJS_For_LangGraph) | The chat app people use the agents in |
| [ZonicMCPServer_FastAPI](https://github.com/TripleQAI/ZonicMCPServer_FastAPI) | The agents' tools over open data |
| **ZonicPostgresqlServer_FastAPI** (this repository) | The PostgreSQL gateway every service stores data through (Neon) |

## Current deployment

Verified on 26 October 2026: the function is active, `GET /ready` reports all five datasources healthy, and the caller keys reach only their own datasources.

| Item | Value |
|---|---|
| Platform | AWS Lambda, account `001634074733` (`tripleq-development`), region `us-east-1` |
| Function | `zonic-postgresql-server` (container image, arm64, 512 MB, 60-second timeout) |
| Public endpoint | Lambda Function URL `https://sdyvf4y53x524cwgdisy2qyfqy0bgjkm.lambda-url.us-east-1.on.aws/` |
| Endpoint authentication | The Function URL is open at the AWS level (`AuthType NONE`, `RESPONSE_STREAM`); every call needs the gateway's caller key as `Authorization: Bearer <key>` |
| Callers | `zonic-agent-builder` (read and write on `agentbuilder`, `neondb`, `neondb_dev`); `zonic-agent-runtime` (read and write on `agentruntime`, `agentobservability`, plus `batch`); `zonic-agent-runtime-node` (read and write on `neondb_dev`, plus `batch`) |
| Container image | ECR repository `zonic-postgresql-server` |
| Last deployment | 26 October 2026 (moved from account `633160612720`, whose deployment was deleted; datasources `agentruntime` and `agentobservability` added) |
| Database | Neon project `rough-wave-74333079`: branch `production` is datasource `neondb`; branch `dev` is datasource `neondb_dev`. Neon project `agent-build-langgraph`, branch `production`: datasources `agentruntime`, `agentobservability` and `agentbuilder` (the builder's workflows since 26 October 2026) |
| Configuration | `postgresqlmodule/code/config/pgfoundation.lambda.yaml` |

- **Deployment files:** `deploy/aws-lambda/` (Dockerfile, `deploy.sh`, README).
- **Redeploy:** `AWS_PROFILE=tripleq-development bash deploy/aws-lambda/deploy.sh`.
- **Secrets:** the caller key and the Neon connection strings are stored in AWS Secrets Manager as `zonic-postgresql-server/runtime` (`us-east-1`), which `deploy.sh` reads first; copies stay in the git-ignored `.env.lambda` and `.env`, and the Lambda function receives them as environment variables. They are never committed. Read them with `aws secretsmanager get-secret-value --secret-id zonic-postgresql-server/runtime --region us-east-1`.
- **Not live:** the Helm chart in `deploy/helm/zonic-postgresql-server` (for EKS) is not deployed yet.

---

## What this project is

A hardened, reusable **infrastructure layer** for talking to PostgreSQL, so every
downstream project shares one correct, fast, observable data-access foundation
instead of re-implementing connection handling, pooling, retries, transactions,
streaming, and configuration.

It owns no tables, ships no migrations, and has no business logic. It is consumed
over its HTTP API, and its recommended surface is **name-based invocation** —
clients name a view, function, or procedure and pass parameters; **no SQL crosses
the wire**.

Two layers, one codebase:

- **`pgfoundation`** — the framework-free core engine: ports & adapters, pooling,
  transactions, streaming, resilience. Ported from the Django repo essentially
  unchanged.
- **`pgfoundation_api`** — a thin **FastAPI (ASGI)** shell exposing the core over
  REST, NDJSON/SSE streams, and a batch endpoint. This is the consumer-facing
  surface, and it is the part being rewritten.

## Technical design

The full technical design is in [docs/technical-design.md](docs/technical-design.md):

- [Architecture](docs/technical-design.md#architecture)
- [The governed surface](docs/technical-design.md#the-governed-surface)
- [Datasources, callers and authorization](docs/technical-design.md#datasources-callers-and-authorization)
- [Request lifecycle](docs/technical-design.md#request-lifecycle)
- [Connection management and performance](docs/technical-design.md#connection-management-and-performance)
- [Security](docs/technical-design.md#security)
- [Observability](docs/technical-design.md#observability)
- [Deployment](docs/technical-design.md#deployment)
- [Testing](docs/technical-design.md#testing)
- [Key design decisions](docs/technical-design.md#key-design-decisions)

## The four requirements, and where each is answered

| # | Requirement | Answer |
|---|-------------|--------|
| 1 | **Same features & functionality** | The core is **ported, not reimplemented** ([ADR-002](documentations/adr/ADR-002-core-ported-unchanged.md)), so parity is structural rather than re-derived. A line-by-line matrix is in [01](documentations/01-scope-and-parity.md), and a parity harness runs every operation through both library mode and service mode. Several features the Django docs designed but never built — streaming, interval, fetch-since, `COPY`, execute-many, atomic batch, real API docs — are delivered here. |
| 2 | **Low code, no hard-coded** | A **declarative operation registry** ([04](documentations/04-declarative-operation-registry.md), [ADR-003](documentations/adr/ADR-003-declarative-operation-registry.md)): one descriptor row per capability generates the REST endpoint, the streaming endpoint, the batch dispatch entry, and the OpenAPI operation. FastAPI deletes the hand-written OpenAPI builder, routing table, and ASGI lifespan loop outright. Every operational value is externalized and validated ([05](documentations/05-configuration.md)). |
| 3 | **Proper design patterns** | The inherited catalog is kept — Ports & Adapters, Facade, Registry, Abstract Factory, Object Pool, Unit of Work, Repository, Strategy, Decorator, Circuit Breaker, Null Object, Bulkhead — and extended with the patterns the new work needs: Command, Scatter–Gather, semaphore-bounded backpressure, Template Method, framework-native DI. Two anti-patterns present in the current code (Service Locator, copy-paste endpoints) are removed. See [03](documentations/03-design-patterns.md). |
| 4 | **High performance, throughput, concurrent, parallel & batch** | A new concurrency layer ([06](documentations/06-concurrency-batch-parallel.md), [ADR-004](documentations/adr/ADR-004-batch-and-parallel-execution.md)): `POST /v1/batch` with three execution strategies (parallel scatter–gather, sequential pipelined, transactional), bounded fan-out, admission control with fast load shedding, constant-memory streaming, psycopg pipeline mode, prepared-statement reuse, `COPY`, orjson, and selectable row formats ([07](documentations/07-performance.md)). Nine concurrency defects found in the current implementation are catalogued and scheduled. |

## Read the plan

Open **[documentations/pgfoundation-fastapi-design.html](documentations/pgfoundation-fastapi-design.html)**
in a browser for the complete plan with navigation and rendered diagrams — or read
the same thing as [one Markdown file](documentations/pgfoundation-fastapi-design.md).
Prefer smaller pieces? [documentations/README.md](documentations/README.md) indexes
the same content as fifteen design documents and eight ADRs.

**Observability** is marked, not implemented: code sketches carry
`# OBSERVE (METRIC|TRACE|LOG):` comments where telemetry belongs, and
[11 — Observability: Instrumentation Points](documentations/11-observability-instrumentation-points.md)
catalogues every emission point, the signal vocabulary, and the redaction and
cardinality rules. `grep -rn "OBSERVE"` is the work list when it comes time to
implement.

The three sections that carry most of the weight:

- [04 — The Declarative Operation Registry](documentations/04-declarative-operation-registry.md) — how "low code" is made concrete and testable.
- [06 — Concurrency, Batch & Parallel](documentations/06-concurrency-batch-parallel.md) — how requirement 4 is delivered.
- [10 — Testing & Delivery Plan](documentations/10-testing-and-delivery-plan.md) — the six phases, and how parity is proven rather than asserted.

## Delivery at a glance

| Phase | Delivers | Exit |
|-------|----------|------|
| **P0** | Port the core engine | Ported core tests pass; architecture contracts green |
| **P1** | Registry-driven FastAPI shell at feature parity | Parity harness green on all five existing endpoints; real Swagger UI |
| **P2** | Batch & parallel execution | Concurrency tests green; parallel batch beats sequential under load |
| **P3** | Streaming (NDJSON/SSE), interval, fetch-since, copy, execute-many | Flat memory over a 10M-row stream; disconnect releases the connection |
| **P4** | Concurrency hardening | Chaos suite green; no leaks in a soak run |
| **P5** | Performance & operations | [07 §7.9](documentations/07-performance.md) budgets met and CI-gated |

## License

Licensed under the Apache License 2.0; see [LICENSE](LICENSE).
