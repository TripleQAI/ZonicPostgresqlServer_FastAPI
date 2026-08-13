# ADR-007 — orjson serialization and selectable row format

**Status:** Accepted
**Date:** 2026-08-13
**Serves:** requirements 1 (contract preservation) and 4 (performance)

## Context

Two related decisions, both on the response hot path.

**Serialization.** Django's `JsonResponse` uses stdlib `json`. For row-heavy
responses, encoding is a meaningful share of total request time — it scales with
result size, unlike routing or validation.

**A contract that must not break.** The current service serializes PostgreSQL
`numeric`/`decimal` as JSON **strings** (`"42.00"`) to preserve exact precision.
This is documented in the README, visible in every example response, and clients
parse it. It works today because psycopg returns `Decimal` and stdlib `json` fails
on `Decimal` — so the DTO or encoder path renders it as a string. **A faster
encoder with different type handling is exactly how this contract gets broken
silently**, turning exact money values into lossy floats without any test failing.

**Row format.** The Django design docs specify `row_format: dict | tuple` in the
request body; it was never implemented. Building one dict per row costs real time
and memory at volume, and repeats every column name in the payload.

## Decision

> **Amended during P1.14 (2026-08-13).** FastAPI now serializes responses through
> Pydantic directly and has **deprecated `ORJSONResponse`**, reporting the native
> path as faster. Both paths were measured against the `Decimal` contract before
> choosing: **both render `Decimal("42.00")` as the string `"42.00"`**. So the
> custom response class is **not** used app-wide — the native path is.
>
> The encoder table below is still built and still required: NDJSON and SSE
> streaming (P3) bypass Pydantic and write bytes themselves, so those paths need
> exactly these rules. They live in `transport/serialization.py` and are applied
> per streamed row rather than per response.
>
> The decision that mattered — *`Decimal` is a string, and that is asserted rather
> than assumed* — is unchanged. Only the mechanism moved.

### Serialization

- **~~`ORJSONResponse` is the default response class~~** → superseded above:
  FastAPI's native Pydantic serialization is the default; the orjson encoder is
  retained for the streaming paths.
- **An explicit encoder table** handles every type psycopg can return that orjson
  does not serialize natively:

| Python type | JSON | Rationale |
|-------------|------|-----------|
| `Decimal` | **string** — `"42.00"` | **Exact precision. Non-negotiable, inherited contract.** |
| `datetime` | RFC 3339 string | orjson native |
| `date` / `time` | ISO string | orjson native |
| `UUID` | string | orjson native |
| `bytes` / `memoryview` | base64 string | JSON has no binary type |
| `Interval` / `timedelta` | ISO-8601 duration string | Explicit rather than accidental |
| `Range`, `Json`, arrays | recursive application of these rules | |

- A contract test asserts the `Decimal` rule directly
  (`test_numeric_serializes_as_string`), and the parity harness compares service
  output against library output for every operation.

### Row format

Selectable per request, defaulting from `api.default_row_format`:

| `row_format` | Response | Mechanism |
|--------------|----------|-----------|
| `dict` (default) | `{"rows": [{"id": 1, "total": "9.90"}], "row_count": 1}` | psycopg `dict_row` — today's behavior, unchanged |
| `tuple` | `{"columns": ["id","total"], "rows": [[1,"9.90"]], "row_count": 1}` | psycopg `tuple_row` — fewer allocations, smaller payload |

This is a **Strategy** chosen at the row-factory level, not a post-processing step
— converting dicts back to tuples in Python would spend the time it is meant to
save.

## Options considered

| Option | Verdict |
|--------|---------|
| **orjson + explicit encoder table** | **Chosen.** Fast, and the contract is stated and tested rather than emergent |
| stdlib `json` (status quo) | Rejected — measurable cost on the one axis that scales with data volume |
| `ujson` | Rejected — weaker type handling, less maintained |
| Pydantic's own serializer for responses | Rejected for row payloads — rows are dynamic `dict[str, Any]`, so per-row model validation would add cost with no schema benefit |
| Floats for `numeric` | **Rejected outright** — silently lossy for money; breaks an existing client contract |
| tuple rows always | Rejected — a breaking change to the default response shape |
| dict rows always (skip `row_format`) | Rejected — leaves a documented feature unbuilt and a real cost unaddressed on export paths |

## Consequences

- **Positive:** faster responses on the axis that matters, with no change to the
  default wire format.
- **Positive:** the `Decimal` rule moves from an implementation accident to a
  stated, tested contract — which is the actual risk this ADR exists to close.
- **Positive:** `tuple` format materially reduces payload size on wide result sets
  (column names appear once, not per row), which compounds with streaming.
- **Negative / mitigation:** two response shapes to document. The OpenAPI response
  model is a union, and `row_format` is documented on every read operation.
- **Negative / mitigation:** orjson is a compiled dependency. It ships prebuilt
  wheels for the supported platforms; if that ever becomes a constraint, the
  response class is a one-line swap back to stdlib `json` with the same encoder
  table.
