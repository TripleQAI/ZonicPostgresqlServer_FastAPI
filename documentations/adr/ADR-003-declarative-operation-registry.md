# ADR-003 — Generate endpoints from a descriptor table

**Status:** Accepted
**Date:** 2026-08-13
**Serves:** requirement 2 (low code, no hard-coding)

## Context

In the Django shell, one capability is declared in four places: a view function, a
`urlpatterns` line, an `apispec` path block, and a DTO. The view functions are
near-identical — *authorize → validate → map DTO to core → call facade → wrap
result* — repeated nine times. Nothing keeps the four declarations consistent.

FastAPI removes two of the four for free (routing is a decorator, OpenAPI is
generated). The question this ADR settles is what to do about the remaining
repetition, and how far to take it.

## Decision

Represent each capability as one **`OperationSpec`** row in an `OPERATIONS` table,
and generate from it:

- the unary REST endpoint,
- the streaming endpoint when `streamable=True`,
- the batch dispatch entry,
- (transitively) the OpenAPI operation.

A single generic endpoint factory holds the five repeated steps exactly once.

**Deliberate limits on the mechanism:**

- The registry generates **transport only**. It never generates SQL. The
  invocation compilers stay hand-written and explicit.
- `OPERATIONS` is a **closed set defined in code**, not a runtime config file.
  Config decides whether a listed operation is *exposed*
  (`api.enable_raw_sql`), never what it *does*. An operator must not be able to
  invent a database operation by editing YAML.
- DTOs stay ordinary, explicitly written Pydantic models. They are the OpenAPI
  contract and must be readable.

## Options considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Descriptor table + endpoint factory** | One row per capability; batch and streaming come free; invariants testable; no magic beyond a closure and `getattr` | Slightly indirect — a reader follows the table to the factory rather than reading a function | **Chosen** |
| Nine hand-written FastAPI endpoints | Maximally explicit; idiomatic | Keeps the repetition requirement 2 objects to; the batch dispatcher would need a *second*, separately-maintained switch over the same operations | Rejected |
| Fully dynamic: operations defined in YAML | "Ultimate" low-code | Turns config into code near a SQL boundary; unreviewable; untypeable; the operations are not actually open-ended (there are ~9 and they change rarely) | Rejected |
| Code generation (emit endpoint source at build time) | Explicit output | Build step, generated files in review, worse errors | Rejected |

## Amendment during implementation (P3.7)

Three of the later operations do not fit `method(one_argument)`: `fetch_since`
takes a cursor **and** a limit, `copy` takes a table **and** rows,
`execute_many` takes a statement **and** a parameter sequence. That left two
options — contort the core so every capability has the same call shape, or let a
row carry a small adapter.

`OperationSpec` gained an optional **`invoke`** callable. The default path is
unchanged (`handle.<method>(to_core(dto))`); a row that needs a different shape
supplies a named adapter function from `mappers.py`. The table stays declarative —
one row, one function reference — and the core keeps signatures that suit its own
callers rather than the registry's convenience.

The consolidation is `spec.call(handle, dto)`: **one** path used by the unary
endpoint, the streaming endpoint, the batch dispatcher and the parity harness
alike. That is what keeps those four surfaces from drifting, and it is stronger
than the original `getattr` because there is now a single place to change.

## Consequences

- **Positive:** adding a capability is one row plus a DTO. Five tests
  ([10 §10.3](../10-testing-and-delivery-plan.md)) enforce that this stays true.
- **Positive:** the batch dispatcher cannot drift from the route table — they read
  the same source.
- **Positive:** the table is a readable inventory of the entire data-plane surface
  in ~15 lines, including which operations are read vs write, streamable, or
  trusted-only.
- **Negative / mitigation — indirection.** A newcomer cannot read one function to
  understand one endpoint. Mitigations: the table is small and heavily commented;
  the factory is ~25 lines; `/docs` shows the concrete result; and the registry
  tests double as executable documentation.
- **Negative / mitigation — dynamic signatures.** Building endpoints whose body
  type varies per spec requires care so FastAPI still introspects them correctly
  (annotations must be set on the generated function, not merely closed over).
  This is a known, contained technique, and the "every operation appears in
  OpenAPI with its request schema" test fails loudly if it is done wrong.
- **Forward value:** a gRPC or Arrow surface later reads the same table, so a
  second transport does not mean a second inventory.
