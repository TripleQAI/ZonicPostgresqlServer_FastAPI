# ADR-001 — Service shell: FastAPI

**Status:** Accepted
**Date:** 2026-08-13
**Relationship to the Django repo:** reinstates the direction of its
`ADR-002 (FastAPI)`, which its `ADR-012` superseded in favor of plain Django.

## Context

The Django repo's ADR-012 chose plain Django for one explicit, legitimate reason:
*"the team knows plain Django only, and wants the OpenAPI feature."* Team fluency
is a real architectural input, and that decision was defensible.

Two things have changed:

1. **A new requirement set.** High throughput, concurrency, parallel calls, and
   batch calls are now first-class ([06](../06-concurrency-batch-parallel.md)).
2. **The cost of that choice is now visible in the code.** Plain Django has no
   OpenAPI generator, so the shell carries a 75-line hand-written `apispec`
   document that duplicates every path already declared in `urls.py` and every
   schema already declared in `schemas.py`. `/docs` is a placeholder HTML page
   with no interactive UI. The ASGI entry point hand-implements the lifespan
   protocol because Django's ASGI handler does not. `INSTALLED_APPS`,
   `MIDDLEWARE`, and `DATABASES` are all empty — the project uses Django as a
   router and nothing else.

The decisive point: **the framework is not load-bearing.** The core is
framework-free and enforced so by an import-linter contract. The shell is a
transport skin. Changing it is exactly the kind of change the hexagonal design was
built to make cheap.

## Decision

Build the service shell on **FastAPI** (Starlette + Pydantic v2) on ASGI.

| Concern | Choice |
|---------|--------|
| Web framework | FastAPI, `async def` endpoints awaiting the async Facade |
| Schemas | Pydantic v2 DTOs (ported from the existing `schemas.py`) |
| OpenAPI | **Generated** from the DTOs and routes — the `apispec` file is deleted |
| Interactive docs | Swagger UI + ReDoc, built in |
| Lifecycle | `@asynccontextmanager` lifespan — the hand-rolled ASGI loop is deleted |
| DI | `Depends` + `app.state` (see ADR-005) |
| Streaming | `StreamingResponse` over the core's existing async generators |
| Serialization | `ORJSONResponse` (see ADR-007) |

The **core library remains framework-free**; the import-linter contract is kept
and simply changes which framework it forbids.

## Options considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **FastAPI** | Async-native; OpenAPI + real docs free; `Depends` gives proper DI; Pydantic v2 validation is Rust-backed; `StreamingResponse` and background/dependency lifecycles fit the batch and streaming work; the deleted code (routing, OpenAPI, lifespan, per-view boilerplate) is exactly the code requirement 2 objects to | New framework for a Django-fluent team | **Chosen** |
| Keep plain Django | Team fluency; no migration | The hand-written OpenAPI, hand-written lifespan, and per-view boilerplate all stay — the code requirement 2 is asking us to remove. Streaming means a second response type; the ORM/admin/template weight stays unused | Rejected |
| Django Ninja | OpenAPI free, Django-based, FastAPI-like API | Adds a dependency the team didn't ask for *and* keeps the Django runtime; strictly dominated by FastAPI for a shell with no ORM, admin, or templates | Rejected |
| Starlette alone | Minimal | We would rebuild request validation and OpenAPI generation — the exact hand-written code we are removing | Rejected |

## Consequences

- **Positive:** three files disappear outright (`openapi.py`, `urls.py`, the ASGI
  lifespan loop) and one shrinks sharply (`views.py`), while the feature set grows
  (streaming, interval, fetch-since, copy, execute-many, batch, real docs).
- **Positive:** `Depends` makes the auth seam a declaration rather than a repeated
  call, and makes per-test app isolation trivial.
- **Positive:** `django` and `apispec` leave `requirements.txt`.
- **Negative / mitigation — the team-fluency cost is real.** Mitigations: the
  concepts the team must learn are narrow (path decorators, `Depends`, Pydantic
  response models); the endpoint factory means most day-to-day work is *editing a
  data table*, not writing FastAPI code ([04](../04-declarative-operation-registry.md));
  the DTOs and the auth module port over nearly verbatim; and the parity harness
  ([10 §10.4](../10-testing-and-delivery-plan.md)) proves behavior is unchanged.
- **Negative / mitigation:** losing Django admin. It is unused here
  (`INSTALLED_APPS` is empty) so nothing is actually lost.
- **Reversibility:** if this proves wrong, the Facade boundary means the shell can
  be re-skinned again without touching the core — which is precisely what this ADR
  is exercising. The operation registry is transport-agnostic, so most of the
  shell's logic would survive the swap.
