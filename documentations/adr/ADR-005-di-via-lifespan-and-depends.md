# ADR-005 — Wiring via lifespan + `app.state` + `Depends`

**Status:** Accepted
**Date:** 2026-08-13
**Serves:** requirements 2 and 3

## Context

The Django shell holds the shared `DataFoundation` and `AuthGate` in
**module-level globals** mutated by setter functions:

```python
_foundation: DataFoundation | None = None
_auth_gate: AuthGate = AuthGate(enabled=False)

def set_foundation(foundation): global _foundation; _foundation = foundation
def get_foundation():
    if _foundation is None: raise RuntimeError("DataFoundation not initialized")
    return _foundation
```

Every view then *locates* its dependencies: `get_foundation()`, `get_auth_gate()`.

This is the **Service Locator** pattern, which the project's own design doc
(`04-design-patterns.md` §4.6, "Anti-patterns we explicitly avoid") names first:

> **Service Locator / global singletons for state** — replaced by explicit DI at
> the composition root.

The practical costs are real, not theoretical:

- Tests mutate process-wide state; ordering and leakage hazards between test
  modules, and no way to run two differently-configured apps in one process.
- The `None` check runs on every request and the failure mode is a `RuntimeError`
  at request time rather than at startup.
- The dependency is invisible in the endpoint signature.

Additionally, `asgi.py` hand-implements the ASGI lifespan protocol — a `while
True` loop over `receive()` matching `lifespan.startup` / `lifespan.shutdown`
messages — because Django's ASGI handler does not support the scope. It also
reaches into `bootstrap._foundation` (the private global) to close it.

## Decision

1. **`create_app(settings) -> FastAPI`** — an application factory. No app is
   constructed at import time.
2. **Lifespan builds the object graph** on the serving event loop and attaches it
   to `app.state`; shutdown drains it with a bounded deadline.
3. **Endpoints declare dependencies** via `Depends`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # OBSERVE (LOG):    service.startup {version, profile, datasources, workers}
    # OBSERVE (METRIC): pgf_startup_duration_seconds
    # OBSERVE: bind the external project's Metrics/Logger/Tracer adapters HERE —
    #          the composition root is the one place that knows the concrete
    #          observability provider (doc 11 §11.11 / ADR-014).
    app.state.foundation = await DataFoundation.from_config(app.state.settings.config_path)
    app.state.auth_gate = build_auth_gate(app.state.settings.auth)
    app.state.limiter = AdmissionLimiter(app.state.settings.concurrency)
    try:
        yield
    finally:
        # OBSERVE (LOG):    service.shutdown {inflight, drained_ms, forced}
        # OBSERVE (METRIC): pgf_shutdown_drain_seconds
        await app.state.foundation.aclose()

def get_foundation(request: Request) -> DataFoundation:
    return request.app.state.foundation

def authorize(action: str):
    async def _dep(request: Request, name: str = Path(...)) -> Principal:
        return await request.app.state.auth_gate.check(request, action=action, resource=name)
    return _dep
```

4. **Authorization becomes a dependency**, not a repeated first line in every
   endpoint body. The registry supplies `spec.action`, so the generated endpoint
   declares `Depends(authorize(spec.action))` and cannot forget the check.
5. **Tests build their own app** with a fake-driver foundation. No globals, no
   setters, full isolation, parallel-test-safe.

The **composition root stays where it belongs** — `DataFoundation.build(...)` is
still the one place that selects the concrete driver. Lifespan calls it; it does
not replace it.

## Options considered

| Option | Verdict |
|--------|---------|
| **Lifespan + `app.state` + `Depends`** | **Chosen.** Framework-native, explicit in signatures, per-app isolation, correct loop affinity |
| Keep module globals, add `Depends` wrappers around them | Rejected — cosmetic; the shared mutable state and its test-isolation problem remain |
| A DI container library (e.g. `dependency-injector`) | Rejected — a dependency for a graph with three nodes; `Depends` already covers it |
| Build the foundation at import time | Rejected — pools would bind to the wrong event loop, which is precisely the failure the current `asgi.py` comment warns about |

## Consequences

- **Positive:** the anti-pattern the project already forbade in writing is now
  forbidden in code.
- **Positive:** an endpoint's dependencies are visible in its signature, and the
  auth check cannot be omitted by forgetting a line.
- **Positive:** ~30 lines of hand-written ASGI protocol handling are deleted.
- **Positive:** test setup becomes `app = create_app(test_settings)` — no global
  mutation, no teardown ordering.
- **Negative / mitigation:** `Depends` is new to a Django-fluent team. It appears
  in exactly one place (the endpoint factory), so most contributors never write it
  by hand.
- **Preserved:** the existing startup behavior that the service comes up even when
  the database is momentarily unreachable, with `/v1/health` reporting
  `degraded`/`down` until it recovers. The lifespan keeps this, and
  [06 §6.9 #5](../06-concurrency-batch-parallel.md) makes the underlying failure
  visible instead of silently swallowed.
