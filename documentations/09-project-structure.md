# 09 — Project Structure & Packaging

## 9.1 Layout

Mirrors the Django repo's shape so the two are diffable side by side, with the
shell package renamed to reflect its framework.

```
ZonicPostgresqlServer_FastAPI/
├─ documentations/                    # ← this design plan
│  ├─ 01..13 *.md
│  └─ adr/
└─ postgresqlmodule/
   ├─ venv314/                        # Python 3.14 virtualenv
   └─ code/                           # the import root
      ├─ config/
      │  ├─ pgfoundation.example.yaml
      │  ├─ pgfoundation.yaml         # local dev (gitignored for real secrets)
      │  └─ profiles/{dev,staging,prod}.yaml
      ├─ pgfoundation/                # === CORE ENGINE (ported, framework-free) ===
      │  ├─ __init__.py               #   the public surface — the only import path
      │  ├─ py.typed
      │  ├─ core/
      │  │  ├─ ports.py               #   Protocols: Connection/Pool/Driver/Logger/Metrics/Tracer/Clock
      │  │  ├─ models.py              #   value objects (Query, Command, ViewQuery, IntervalQuery, …)
      │  │  └─ errors.py              #   driver-agnostic error hierarchy
      │  └─ _internal/                #   PRIVATE — no stability promise
      │     ├─ config/                #   L0 loader chain + typed AppSettings
      │     ├─ connection/            #   L1 ConnectionRegistry + PoolFactory
      │     ├─ access/                #   L2 uow, transactions, invocation, interval, watermark, sql, repository
      │     ├─ api/                   #   L3 DataFoundation facade + DataSourceHandle (composition root)
      │     ├─ drivers/               #   psycopg adapter + errors_map + FakeDriver
      │     ├─ resilience/            #   circuit breaker + ResilientPool decorator
      │     ├─ observability/         #   MeteredPool/Connection + Null Objects
      │     └─ clock.py
      ├─ pgfoundation_api/            # === SERVICE SHELL (FastAPI, ASGI) ===
      │  ├─ app.py                    #   create_app(settings) — assembles everything below
      │  ├─ settings.py               #   ServiceSettings (pydantic-settings) + profile overlay
      │  ├─ errors.py                 #   error→status table + registered exception handlers
      │  ├─ run_local.py              #   dev launcher (Windows SelectorEventLoop guard)
      │  │
      │  ├─ bootstrap/                #   COMPOSITION ROOT — what gets built, and when
      │  │  ├─ lifespan.py            #     build the foundation on the serving loop; drain on exit
      │  │  ├─ deps.py                #     Depends providers: foundation, settings, limiter, principal
      │  │  └─ providers.py           #     plug points: bind external observability + auth adapters
      │  │
      │  ├─ registry/                 # ★ THE LOW-CODE ENGINE — version-AGNOSTIC machinery
      │  │  ├─ spec.py                #     OperationSpec — the descriptor contract
      │  │  ├─ endpoints.py           #     the generic endpoint factory (5 repeated steps, once)
      │  │  └─ router.py              #     build_router(operations, prefix, settings)
      │  │
      │  ├─ api/                      #   VERSIONED SURFACE — one package per API version
      │  │  ├─ versions.py            #     VERSIONS = {"v1": v1.build}; app.py mounts each
      │  │  ├─ v1/
      │  │  │  ├─ __init__.py         #       build(settings) -> APIRouter(prefix="/v1")
      │  │  │  ├─ operations.py       #     ★ the OPERATIONS table for v1 (pure data)
      │  │  │  ├─ mappers.py          #       to_core(): DTO → core value object, v1 semantics
      │  │  │  ├─ schemas/            #       the v1 wire contract — frozen once shipped
      │  │  │  │  ├─ common.py        #         Filter, OrderBy, RowFormat, shared field types
      │  │  │  │  ├─ invocation.py    #         ViewIn, FunctionIn, ProcedureIn, IntervalIn, FetchSinceIn
      │  │  │  │  ├─ bulk.py          #         ExecuteManyIn, CopyIn, QueryIn (raw SQL — trusted)
      │  │  │  │  ├─ batch.py         #         BatchRequest, BatchItem, BatchResult, BatchItemResult
      │  │  │  │  └─ responses.py     #         QueryOut, PageOut, ExecuteOut, HealthOut, ErrorOut
      │  │  │  └─ routes/             #       the ONLY hand-written v1 routes
      │  │  │     ├─ admin.py         #         /v1/datasources, /v1/health, /v1/ready
      │  │  │     ├─ batch.py         #         POST /v1/batch
      │  │  │     └─ subscribe.py     #         GET /v1/datasources/{name}/subscribe/{channel}
      │  │  └─ (v2/ lands here later, same shape — see §9.1b)
      │  │
      │  ├─ batch/                    #   ORCHESTRATION ONLY — version-agnostic, no pool access
      │  │  ├─ dispatch.py            #     item dispatch (Command); receives the table, never imports it
      │  │  ├─ limits.py              #     semaphores, admission, per-item + total deadlines
      │  │  └─ strategies/            #     Strategy — one file each, selected by `mode`
      │  │     ├─ base.py             #       protocol + shared envelope assembly
      │  │     ├─ parallel.py         #       bounded scatter–gather across N pools
      │  │     ├─ sequential.py       #       one pinned connection, psycopg pipeline mode
      │  │     └─ transactional.py    #       one UnitOfWork, retried whole on 40001/40P01
      │  │
      │  ├─ transport/                #   how results become bytes
      │  │  ├─ serialization.py       #     ORJSONResponse + encoder table (Decimal → string)
      │  │  └─ streaming.py           #     NDJSON + SSE framing over the core's async generators
      │  │
      │  ├─ middleware/               #   cross-cutting; one concern per file, order set in app.py
      │  │  ├─ correlation.py         #     X-Request-Id in/out; thread into PG application_name
      │  │  ├─ admission.py           #     global + per-datasource limiter; fast 503 + Retry-After
      │  │  └─ timing.py              #     duration + in-flight metrics
      │  │
      │  ├─ security/                 #   the auth boundary
      │  │  ├─ ports.py               #     AuthenticatorPort, AuthorizerPort, Principal
      │  │  ├─ gate.py                #     AuthGate — fail-closed; AllowAll defaults
      │  │  └─ policy.py              #     caller → {datasource} × {action}, from config
      │  │
      │  └─ ops.py                    #   UNVERSIONED: /health, /ready, /metrics — probe targets
      │                               #   that must not be pinned to an API version (§9.1b)
      ├─ tests/
      │  ├─ unit/                     #   FakeDriver; no database
      │  ├─ service/                  #   httpx.AsyncClient against the app; no database
      │  ├─ contract/                 #   library-mode vs service-mode parity + OpenAPI snapshot
      │  ├─ integration/              #   live PostgreSQL (skipped without PGF_TEST_DSN)
      │  └─ load/                     #   k6 / locust scripts + asyncio harness
      ├─ .importlinter
      ├─ pyproject.toml
      └─ requirements.txt
```

★ = the package that makes requirement 2 real.

## 9.1a Why the shell is shaped this way

The shell is grouped **by architectural role**, and every subpackage answers one
question:

| Subpackage | Answers |
|------------|---------|
| `bootstrap/` | *What is built, when, and what plugs into it?* |
| `registry/` | *How does a descriptor become a route?* — the machinery, shared by every API version |
| `api/vN/` | *What capabilities exist in version N, and what is their wire contract?* |
| `api/vN/routes/` | *What could not be generated?* |
| `batch/` | *How are N operations scheduled?* |
| `transport/` | *How do results become bytes?* |
| `middleware/` | *What happens to every request regardless of route?* |
| `security/` | *Who is calling, and may they?* |
| `ops.py` | *Is the process alive?* — unversioned, for probes and scrapers |

Three of these groupings do real work beyond tidiness:

- **The low-code machinery is split from the low-code data.** `registry/` holds
  the descriptor contract, the endpoint factory and the router builder — none of
  which know anything about a version. `api/v1/operations.py` holds the table
  itself: pure data, no logic. That separation is what makes "adding a capability
  is adding a row" true rather than aspirational, and it is also what lets a
  future `v2` reuse the entire engine (§9.1b).
- **`api/v1/routes/` holds only what could not be generated.** Three files. If a
  fourth appears, that is a design smell worth a conversation — someone bypassed
  the registry. The directory listing itself is the review signal, and the
  `test_no_route_outside_the_registry` check in
  [10 §10.3](./10-testing-and-delivery-plan.md) is its enforcement.
- **`batch/strategies/` makes the Strategy pattern physical.** One file per mode,
  each ~50–80 lines of genuinely different logic. A fourth mode is a fourth file,
  not a fourth branch in a growing `strategies.py`.

### The splitting rule

So the structure can be extended consistently rather than by taste:

> **One subpackage per architectural role. A module splits only when it holds
> independent variation** — a Strategy, a middleware, a DTO family, a pluggable
> provider — **or would otherwise exceed ~150 lines.**

By that rule `errors.py` stays a single module (~45 lines, one table plus its
handlers) while `middleware/` is a package (three concerns that are enabled,
ordered and tested independently).

### A word on granularity

This is ~30 modules for a shell estimated at roughly 1,200–1,500 lines, so the
average module is small. That is deliberate for a *declarative* shell — most files
are tables, DTOs, or one small class — but it is a real trade-off, and if it reads
as ceremony to the team the merges to make first are: fold `registry/spec.py` into
`operations.py`, `registry/mappers.py` into `registry/endpoints.py`, and
`schemas/common.py` into `schemas/invocation.py`. That drops three files without
touching the architecture. Do **not** flatten `registry/`, `batch/strategies/` or
`routes/` — those three carry meaning, not just files.

### The alternative that was rejected (structure)

The other legitimate organization is **vertical slices** — `capabilities/view.py`
holding that operation's DTO, mapper and registry row together, so a capability
lives in exactly one file. It was rejected because it fragments the two things
that most benefit from being read whole: the `OPERATIONS` table (a 15-line
inventory of the entire data plane) and the DTO set (the OpenAPI contract). The
horizontal split keeps both readable in one place; the cost is that adding a
capability touches a row plus a DTO plus a mapper in three files, which the
registry tests already keep honest.

## 9.1b API versioning — what a `v2` actually costs

The surface is mounted under `/v1`, so a `v2` must be possible without a rewrite.
The structural answer is the `api/vN/` package above, but the reasoning matters
more than the folder, because **the obvious version boundary is the wrong one.**

### Why versioning `routes/` alone would be a trap

`routes/` holds three hand-written endpoints. Everything else — view, function,
procedure, interval, fetch-since, execute-many, copy, raw query/execute, plus all
their `/stream` variants — is **generated from the registry**, and its URL prefix
came from a hard-coded `APIRouter(prefix="/v1/datasources")`. Versioning only the
folder would leave the generated majority pinned to v1 forever.

Worse, `routes/` is not even where breakage lives. A new major version is caused by
a **wire-contract** change, and the wire contract is `schemas/` — renaming a
response field, dropping a DTO field, or changing `numeric` from a JSON string to a
number. So the version boundary has to enclose the DTOs, the table, the mappers
**and** the routes. That is exactly what `api/v1/` does.

### What forks, and what never does

| Forks per version | Never forks |
|-------------------|-------------|
| `api/vN/schemas/` — the wire contract | `registry/` — spec, endpoint factory, router builder |
| `api/vN/operations.py` — which capabilities exist | `batch/` — strategies, limits, dispatch |
| `api/vN/mappers.py` — DTO → core semantics | `transport/`, `middleware/`, `security/`, `errors.py`, `bootstrap/` |
| `api/vN/routes/` — hand-written endpoints | `ops.py` — unversioned by design |
| | **The core engine.** `pgfoundation` has its own SemVer and no concept of an HTTP version |

That last row is the important one: **a new API version is a shell concern only.**
v1 and v2 are two mappings onto the same Facade, sharing one process, one
`DataFoundation`, and one set of connection pools. Two API versions must never
mean two deployments or two pool sets.

Three consequences to keep true while building v1, so v2 stays cheap:

1. **`build_router(operations, prefix, settings)` takes its prefix and table as
   arguments.** No `/v1` string anywhere in `registry/`.
2. **`batch/dispatch.py` receives the operations table**; it never imports it.
   Otherwise the version-agnostic batch layer would be pinned to v1's capabilities.
3. **`ops.py` is unversioned.** `/health`, `/ready` and `/metrics` are probe and
   scrape targets. A Kubernetes liveness probe pinned to `/v1/health` has to be
   edited the day v1 is retired — precisely the day you least want to be touching
   probes. `/v1/health` and `/v1/ready` remain as documented aliases for parity
   with the Django surface ([01](./01-scope-and-parity.md)).

### When a v2 is justified

Per [08 §8.5](./08-api-surface.md), additive changes — a new operation, a new
optional field — ship **within v1**. A v2 exists only for a breaking change:
a renamed or removed response field, changed serialization semantics, a changed
error mapping, or a changed default. The registry invariant tests and the OpenAPI
snapshot make an accidental break a failing test rather than a surprise v2.

### How a v2 would actually be written

Not by copying v1. It starts as a delta:

```python
# api/v2/operations.py
from ..v1.operations import OPERATIONS as V1
from .schemas.responses import QueryOutV2

OPERATIONS = tuple(
    replace(spec, response_model=QueryOutV2) if spec.op in CHANGED else spec
    for spec in V1
)
```

Only the rows that actually change are overridden. The engine, the batch layer and
the security model are reused untouched — which is the whole point of having split
machinery from data.

### Running two versions at once

- `app.py` mounts each entry in `api/versions.py`, so adding v2 is one dict entry.
- **Separate OpenAPI documents per version** — `/v1/openapi.json`,
  `/v2/openapi.json`. A merged spec containing two conflicting `QueryOut` schemas
  is unusable for client generation.
- **Deprecation is data, not lore.** The old version emits `Deprecation` and
  `Sunset` headers, with the dates in config — no hard-coded date, per
  [05](./05-configuration.md).
- **Add a `version` label to `pgf_http_requests_total`** ([11](./11-observability-instrumentation-points.md)).
  It is a bounded label, and it is the only way to answer "is anyone still on v1?"
  before switching v1 off. Retiring a version on a guess is how you find your
  remaining callers the hard way.

## 9.2 What moves, what is written fresh

| Source (Django repo) | Disposition |
|----------------------|-------------|
| `pgfoundation/**` | **Copied**, plus the fixes in [06 §6.9](./06-concurrency-batch-parallel.md) and the additive `pipeline()` / row-factory seams |
| `pgfoundation_service/views.py`, `urls.py`, `openapi.py` | **Replaced** by `registry/` (the descriptor table generates all three) |
| `pgfoundation_service/asgi.py`, `bootstrap.py` | **Replaced** by `app.py` + `bootstrap/` — the hand-rolled ASGI lifespan loop and the global singletons both go ([ADR-005](./adr/ADR-005-di-via-lifespan-and-depends.md)) |
| `pgfoundation_service/settings.py` | **Replaced** by `settings.py` (`ServiceSettings`, no Django settings module) |
| `pgfoundation_service/schemas.py` | **Ported and extended**, split into `api/v1/schemas/` (interval, fetch-since, copy, execute-many, batch DTOs) |
| `pgfoundation_service/http_errors.py` | **Ported** to `errors.py` — the status table is kept; the per-view decorator becomes exception handlers |
| `pgfoundation_service/auth.py` | **Ported ~verbatim** into `security/ports.py` + `security/gate.py`; call sites become one `Depends`. `security/policy.py` is new |
| `config/*.yaml` | **Ported and extended** ([05](./05-configuration.md)) |
| `tests/**` | **Ported**; Django-specific fixtures (`django.setup()`, `AsyncRequestFactory`) replaced by an app factory + `httpx.AsyncClient` |
| `.importlinter` | **Ported**, with the forbidden web framework changed from `django` to `fastapi`/`starlette` |

## 9.3 Architecture contracts (import-linter, CI-enforced)

Carried over, updated for the new package names, plus one new contract for the
registry:

```ini
[importlinter:contract:layers]
type = layers
layers =
    _internal.api
    _internal.access
    _internal.connection
    _internal.config
    core
containers = pgfoundation

[importlinter:contract:core-purity]
type = forbidden
source_modules = pgfoundation.core
forbidden_modules = psycopg, psycopg_pool, fastapi, starlette, pydantic

[importlinter:contract:driver-isolation]
type = forbidden
source_modules =
    pgfoundation.core
    pgfoundation._internal.config
    pgfoundation._internal.connection
    pgfoundation._internal.access
    pgfoundation._internal.resilience
    pgfoundation._internal.observability
forbidden_modules = psycopg, psycopg_pool
# _internal.api is the composition root — the one place allowed to select a driver.

[importlinter:contract:no-web-framework-in-core]
type = forbidden
source_modules = pgfoundation
forbidden_modules = fastapi, starlette, uvicorn

[importlinter:contract:shell-uses-public-surface-only]   # NEW
type = forbidden
source_modules = pgfoundation_api
forbidden_modules = pgfoundation._internal
# The shell must go through the public Facade — this is what guarantees
# library/service parity. Test fixtures are the only exception, and they live in
# tests/, not in the shell.

[importlinter:contract:shell-layers]                     # NEW
name = The shell's own layering — dependencies point down
type = layers
layers =
    api
    batch
    registry
containers =
    pgfoundation_api
# A version package may use batch; batch may use the registry; nothing points back
# up. Concretely this forbids the two mistakes that would quietly dissolve the
# structure — and with it any hope of a cheap v2:
#   * the registry importing a version package (it must stay version-agnostic), and
#   * batch importing a version package (it receives the operations table instead).

[importlinter:contract:version-independence]             # NEW — add v2 when it lands
name = API versions never import each other's runtime
type = independence
modules =
    pgfoundation_api.api.v1
#   pgfoundation_api.api.v2
# Deliberate exception: a new version's `operations.py` may import the PREVIOUS
# version's table to express itself as a delta (§9.1b). Whitelist that one edge
# explicitly rather than dropping the contract — an unrestricted import path
# between versions is how "two versions" quietly becomes "one version with
# branches".

[importlinter:contract:cross-cutting-are-leaves]         # NEW
type = forbidden
source_modules =
    pgfoundation_api.middleware
    pgfoundation_api.security
    pgfoundation_api.transport
forbidden_modules =
    pgfoundation_api.api
    pgfoundation_api.batch
    pgfoundation_api.registry
# Cross-cutting packages are used BY the request path, never the reverse.
```

That last contract is new and important: today `pgfoundation_service`'s test
conftest imports `pgfoundation._internal.drivers.fake` and
`pgfoundation._internal.config.loader`. That is fine in tests; it must not be
possible in the shell itself.

## 9.4 Dependencies

```
# --- core engine ---
psycopg[binary,pool]>=3.2
pydantic>=2.7
pyyaml>=6.0

# --- service shell ---
fastapi>=0.115
pydantic-settings>=2.3
orjson>=3.10
uvicorn[standard]>=0.30       # brings httptools + uvloop (POSIX)

# --- dev / test ---
pytest>=8.0
pytest-asyncio>=0.23
httpx>=0.27                   # ASGI test client
import-linter>=2.0
pytest-benchmark>=4.0
```

**Dropped:** `django`, `apispec` (OpenAPI is generated). Net dependency count goes
down while the feature set goes up.

## 9.5 Not an installable package

Unchanged from today: `pgfoundation` and `pgfoundation_api` are **not**
pip-installable distributions. They are imported from `postgresqlmodule/code`,
which the entry points put on `sys.path` themselves. `pyproject.toml` carries
tooling config only (pytest, ruff, mypy). The service is consumed over HTTP.

## 9.6 Running it

```bash
cd postgresqlmodule/code
export PGF_CONFIG="$PWD/config/pgfoundation.yaml"
export PGF_SERVER__PORT=8600
../venv314/Scripts/python.exe -m pgfoundation_api.run_local
```

or any ASGI server:

```bash
python -m uvicorn pgfoundation_api.app:create_app --factory --port 8600 --workers 4
```

`--factory` matters: `create_app(settings)` builds the application per worker, so
no app object exists at import time and each worker's pools bind to its own event
loop ([ADR-005](./adr/ADR-005-di-via-lifespan-and-depends.md)).

Two properties are preserved from the current runner:

- **Windows needs a `SelectorEventLoop`** — async psycopg cannot run on the
  Proactor loop. `run_local.py` builds the loop itself rather than letting uvicorn
  choose.
- **Pools are built on the serving loop** via the lifespan, never at import time —
  otherwise they bind to the wrong loop and fail at first use.

## 9.7 Tooling baseline

| Concern | Tool |
|---------|------|
| Lint / format | Ruff |
| Types | mypy or pyright, strict on the public surface |
| Architecture rules | import-linter |
| Tests | pytest, pytest-asyncio, httpx, pytest-benchmark |
| Load | k6 or Locust |
| Security | pip-audit, bandit, secret scanning |
