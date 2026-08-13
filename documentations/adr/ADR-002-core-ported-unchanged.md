# ADR-002 — Port the core engine unchanged; rewrite only the shell

**Status:** Accepted
**Date:** 2026-08-13

## Context

"Rebuild the project on FastAPI" could mean anything from *re-skin the transport*
to *start over*. The scope has to be decided explicitly, because requirement 1 is
**same features and functionality** — and a rewrite is the most reliable way to
lose features quietly.

The existing `pgfoundation` core is ~1,900 lines that already satisfy the
architectural requirements: ports & adapters, one-directional layering enforced by
import-linter, no framework imports, no ORM, driver-agnostic errors, and a fake
driver that lets the entire stack be tested with no database. It has 55 passing
tests. Its invocation compilers are a reviewed security boundary.

None of that is Django-shaped. There is nothing to port *away from*.

## Decision

**Copy the core engine essentially verbatim.** Rewrite only `pgfoundation_service`
→ `pgfoundation_api`.

Three narrow classes of core change are allowed:

1. **Concurrency-correctness fixes** — the defect list in
   [06 §6.9](../06-concurrency-batch-parallel.md): breaker synchronization, unique
   cursor names, parallel `health`/`start`/`aclose`, decorator unwrap contract,
   warm-up error visibility. These are bugs that only matter at the concurrency
   the new requirements ask for.
2. **Additive seams** — `ConnectionPort.pipeline()` for pipeline mode, a row-factory
   selector for `row_format`, `itersize` and `prepare` from config. Additive:
   existing signatures keep their behavior.
3. **Nothing else.** In particular the invocation compilers
   (`compile_view` / `compile_function` / `compile_procedure` / `compile_interval`)
   and `safe_identifier` are **frozen**. They are the injection boundary and they
   are well tested.

## Options considered

| Option | Verdict |
|--------|---------|
| **Port the core, rewrite the shell** | **Chosen.** Confines risk to the layer that must change; requirement 1 is satisfied by construction rather than by re-derivation |
| Rewrite everything "properly" on FastAPI-native idioms | Rejected. There is no FastAPI-native idiom for a connection pool. It would re-litigate settled decisions, discard 55 passing tests, and put the security-critical compilers back on the table |
| Replace the core with SQLAlchemy Core / an existing library | Rejected. The project's stated purpose is to *be* the shared foundation; the non-goals explicitly exclude an ORM, and SQLAlchemy would not provide the governed name-based invocation surface that is this project's distinguishing feature |

## Consequences

- **Positive:** requirement 1 is largely satisfied on day one of P0; the parity
  harness then proves it for the HTTP surface.
- **Positive:** all core tests port unchanged, so the port starts from a green
  suite rather than an empty one.
- **Positive:** the security boundary is not re-implemented under time pressure.
- **Negative / mitigation:** the core keeps a few rough edges (e.g. `execute()`
  routing writes through `query()`, which discards write intent before the pool).
  These are catalogued in [06 §6.9](../06-concurrency-batch-parallel.md) and
  scheduled into P4 rather than fixed opportunistically during the port — so a
  behavior change is never mistaken for a port artifact.
- **Consequence for reviewers:** a P0 diff of the core against the Django repo
  should be near-empty. If it isn't, that is a review finding.
