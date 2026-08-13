# P0 port findings — pre-existing issues in the ported core

The core engine was ported **verbatim** and the P0.8 fidelity gate asserts it is
byte-identical to `ZonicPostgresqlServer_DjangoApp/postgresqlmodule/code/pgfoundation`
([ADR-002](../../documentations/adr/ADR-002-core-ported-unchanged.md)).

Static analysis on the ported code surfaced the findings below. **None are fixed
in P0** — fixing them would trade away the "the core is unchanged" guarantee that
requirement 1 rests on, and a behaviour change during a port is indistinguishable
from a port defect. Each is assigned to the phase that already has to touch that
code.

Reproduce: `../venv314/Scripts/mypy.exe pgfoundation`

## Type findings (7)

| # | Location | Finding | Assessment | Scheduled |
|---|----------|---------|-----------|-----------|
| 1 | `_internal/api/handle.py:146` | `ConnectionPort` has no attribute `copy_stream` | **Real gap.** `DataSourceHandle.copy_stream()` calls `conn.copy_stream()`, and both the psycopg driver and `FakeDriver` implement it — but the `ConnectionPort` Protocol in `core/ports.py` never declares it. The port is incomplete, so the type checker cannot verify any adapter actually satisfies the contract the handle relies on. | **P2.11** — that task already extends `ConnectionPort` with `pipeline()`; declare `copy_stream` and `listen` in the same change |
| 2 | `_internal/api/handle.py:154` | `ConnectionPort` has no attribute `listen` | Same as #1. | **P2.11** |
| 3 | `_internal/connection/registry.py:52` | `MeteredPool` is not a `PoolPort`: `release(conn: MeteredConnection)` narrows `release(conn: ConnectionPort)` | **Real, and already known** — this is defect #6 in [06 §6.9](../../documentations/06-concurrency-batch-parallel.md), the `conn._inner` unwrap. mypy found it independently. The decorator narrows its parameter type, which is unsound: it only works because callers happen to pass back the exact object `acquire()` returned. | **P4.2** — "explicit decorator unwrap contract" |
| 4 | `_internal/api/facade.py:59` | `PsycopgDriver` assigned to `DriverPort \| None` | Narrowing noise: `driver` is the parameter, reassigned inside `if driver is None`. Harmless at runtime; a local variable would type cleanly. | **P4** cleanup |
| 5 | `_internal/api/facade.py:63` | `DriverPort \| None` passed where `DriverPort` expected | Consequence of #4. | **P4** cleanup |
| 6 | `core/models.py:208` | `ResultSet.__iter__` missing a type annotation | **Public surface.** Only visible with `disallow_untyped_defs`. Trivial: `-> Iterator[Row]`. | **P4** — with the strictness flip |
| 7 | `core/ports.py:76` | `TracerPort.span` missing a return type | **Public surface**, same as #6. Should be `AbstractContextManager[Any]`. Do it **before P5.4** binds real tracer adapters against this port. | **P4** — with the strictness flip |

### Why the public-surface strict gate is not on yet

[09 §9.7](../../documentations/09-project-structure.md) requires strict typing on
the public surface. Enabling `disallow_untyped_defs` today surfaces findings #6
and #7, which leaves only bad options: a red gate from P0 (forbidden by standing
rule [13 §13.2](../../documentations/13-implementation-plan.md)) or editing the
core (forbidden by ADR-002).

So the flip is **scheduled into P4 alongside the two annotations** — the override
is present but commented in `pyproject.toml` with a pointer. Meanwhile mypy runs
at default strength over the whole package and is green, so it still blocks *new*
type errors in the shell built from P1. The debt is visible in config and here,
rather than being quietly claimed as done.

## CI gate shape

| Gate | Scope | Blocking |
|------|-------|----------|
| `ruff check .` | everything (ported paths carry documented per-file ignores) | ✅ |
| `ruff format --check .` | new code only; ported paths excluded | ✅ |
| `mypy pgfoundation/core --follow-imports=silent` | public surface | ✅ — green |
| `mypy pgfoundation` | whole package | ⚠️ report-only — the 5 findings above |
| `lint-imports` | 4 architecture contracts | ✅ |
| `pytest -q` | 51 unit + 4 integration (skipped without `PGF_TEST_DSN`) | ✅ |

## Lint findings (36, all style)

`ruff` flags 36 style modernizations in ported paths — `typing.Sequence` over
`collections.abc` (UP035), quoted annotations that `from __future__ import
annotations` makes unnecessary (UP037), `(str, Enum)` over `StrEnum` (UP042),
PEP 695 generics (UP046), import ordering, and a few `SIM` simplifications.

All are genuine improvements and **none is a defect**. They are silenced for
ported paths only in `pyproject.toml` (`[tool.ruff.lint.per-file-ignores]`); new
code is held to the full ruleset. `ruff format` is likewise excluded for ported
paths — reformatting 33 files would destroy the fidelity diff for cosmetics.

**Scheduled: P4**, as a standalone mechanical change once the port is no longer
the thing under review, so its diff stays reviewable on its own.

## Test-harness note

`tests/conftest.py` overrides the `event_loop_policy` fixture, which
`pytest-asyncio` 1.4 deprecates in favour of the `pytest_asyncio_loop_factories`
hook. It **works today** (51 passed, 4 skipped) but will break on a future
upgrade. Left as-is for fidelity; the migration belongs with the P1 test work,
which rewrites the service-test fixtures anyway.

## What is NOT in this list

The [06 §6.9](../../documentations/06-concurrency-batch-parallel.md) concurrency
defects — unsynchronized circuit-breaker state, `id(spec)` cursor naming, serial
`health`/`start`/`aclose`, swallowed warm-up errors — are design findings from
reading the code, not static-analysis output. They keep their existing schedule:
parallelism fixes in **P2.1**, the rest in **P4.1–P4.7**.
