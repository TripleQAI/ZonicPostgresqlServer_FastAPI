# 12 — Deployment Targets & Environments

Where the foundation actually connects. Structure and tunables live here; **no
credential appears in this repository** ([05](./05-configuration.md)).

## 12.1 Target inventory

| Env | Provider | Host | Port | Database | Login role | Notes |
|-----|----------|------|------|----------|-----------|-------|
| **`tsdb`** (`tripleq-development`) | **Timescale Cloud** (AWS `us-east-1`) | `aqy26lurzx.lhy6ly0as0.tsdb.cloud.timescale.com` | `32064` | `tsdb` | `tsdbadmin` (admin — see §12.5) | **Direct session endpoint, no pooler** (§12.6). 2 vCPU. TLS verified (§12.3). Reached over the public internet |
| local *(optional)* | Docker `timescale/timescaledb:latest-pg16` | `localhost` | **`5434`** (host) -> 5432 (container) | `energydata` (+ 6 others) | `fshadmin` | PostgreSQL 16.14, timescaledb 2.29.1, `max_connections=100`. The migration **source**; not enabled by default. Objects live in `client` / `dbo` / `fsh` / `utility`, **not `public`** — see below |

The service URL as supplied:

```
postgres://tsdbadmin@aqy26lurzx.lhy6ly0as0.tsdb.cloud.timescale.com:32064/tsdb?sslmode=require
```

> **The console's Service URL contains no password** — there is nothing between
> `tsdbadmin` and `@`. Pasted verbatim, libpq reports
> `fe_sendauth: no password supplied` rather than an authentication error, which
> reads like a connectivity problem and is not. The password is shown once at
> service creation; otherwise reset it via **Forgot your password?** in the
> Connect panel. `python check_dsn.py` at the repo root catches this and the
> related percent-encoding traps before a server is started.

### `tsdb` is the cloud service

`config/pgfoundation.yaml` defines **`tsdb` = Tiger Cloud** — the deployed
target, and the only data source enabled by default. Its pool is tuned for a
database across the public internet rather than a LAN: warm connections
(`min_size: 2`, because each one costs a TLS handshake to a remote host), a 10 s
acquire timeout, a 30 s statement timeout, and `itersize: 5000` for fewer,
larger fetches.

> **Naming convention: the alias is the database name.** The Django project
> used `main` for the *local Docker* instance; this project pointed the same
> word at the *cloud* service. A saved `/v1/datasources/main/view` request
> therefore kept working while reading an entirely different database — the
> request looked identical and the answer was not.
>
> Aliases are now named after the database they open (`tsdb`, `energydata`), so
> a URL states which database it reads. Renaming an alias breaks callers loudly,
> which is the correct failure; silently repointing one does not.

The local Docker instance remains available as an optional **second** data
source (`energydata`, commented out in the config). Enabling it gives each
source its own pool and circuit breaker, so saturating one cannot starve the
other — managing N named sources from one process being what this layer is for
(Requirement 1). That is the shape a migration wants: read from `energydata`,
write to `tsdb`, in one process.

### ⚠ If you enable `energydata`: it has no `public` schema

Its objects live in `client`, `dbo`, `fsh` and `utility`, and `search_path` is
`"$user", public`. Name-based calls against it **must** qualify the schema:

```bash
# works
-d '{"name":"facility","schema":"client","limit":2}'

# fails: relation "facility" does not exist
-d '{"name":"facility","limit":1}'
```

Known objects: `client.facility`, `dbo.userfacilitymapping`,
`fsh.degreeday_hourly_temps`, `fsh.degreeday_monthly`, `fsh.weatherdata`,
`utility.bill`, `utility.bill_reference_summary`. Note there are **no
hypertables** in `energydata` despite TimescaleDB being installed — consistent
with the cloud service, and with time-series staying deferred (§12.7).

## 12.2 How the DSN reaches the service

Never inline, never committed. The config file holds a **reference**; the loader
chain resolves it ([05 §5.4](./05-configuration.md)):

```yaml
# config/pgfoundation.yaml — committed, no secrets
datasources:
  - name: tsdb
    dsn: ${env:PGF_TSDB_DSN}          # or ${secret:tsdb/dsn} once a resolver is bound
    role: standalone
    statement_timeout_ms: 30000        # cloud RTT — see §12.6
    pool:
      min_size: 2                      # keep connections warm; connect cost is high here
      max_size: 10                     # see the connection budget, §12.4
      acquire_timeout_seconds: 5
      max_lifetime_seconds: 1800
    stream:
      itersize: 5000                   # fewer, larger fetches across a WAN
```

```bash
# supplied by the platform / secret manager — never by a file in this repo
export PGF_TSDB_DSN="postgresql://<role>:<password>@aqy26lurzx.lhy6ly0as0.tsdb.cloud.timescale.com:32064/tsdb?sslmode=verify-full&application_name=pgfoundation"
```

`dsn` is a `SecretStr`, so it stays out of `repr`, logs, error payloads and the
OpenAPI document — asserted by
`test_dsn_is_secret_and_not_leaked_in_repr` and the redaction suite
([11 §11.8](./11-observability-instrumentation-points.md)).

## 12.3 TLS: prefer `verify-full` over `require`

**Verified against this endpoint (2026-08-14):** the certificate presents
`CN=aqy26lurzx.lhy6ly0as0.tsdb.cloud.timescale.com`, is issued by Google Trust
Services, expires 2026-11-10, and **validates against the system trust store** —
so `verify-full` works with no CA bundle and no `sslrootcert=`. There is no cost
to using it.

The console's Service URL uses `sslmode=require`. That **encrypts the connection but does
not authenticate the server** — it accepts any certificate, so it does not defend
against an active man-in-the-middle. Over the public internet that gap is worth
closing:

| Mode | Encrypts | Verifies the server | Verdict |
|------|----------|--------------------|---------|
| `require` | yes | **no** | Works; acceptable only as a stopgap |
| `verify-ca` | yes | chain only, not hostname | Partial |
| `verify-full` | yes | chain **and** hostname | **Recommended** |

For **this** endpoint no `sslrootcert=` is needed — the chain is publicly rooted
and already trusted (measured above), so `verify-full` costs nothing but a
different word in the DSN. Every profile should assert it, including dev: there
is no trade-off left to make.

Treat this as a config change, not a code change — nothing in the foundation
parses the DSN.

## 12.4 Connection budget — the arithmetic that must be checked

The service is a **connection concentrator**, so its pools are the *only*
consumer of the database's connection slots. The number that matters:

```
total connections = uvicorn workers × Σ (datasource pool.max_size)
```

Each worker builds its **own** pools ([07 §7.8](./07-performance.md)), so 4
workers × `max_size: 10` is **40** connections, not 10. Managed instances cap
connections by plan tier, and Timescale Cloud is no exception.

**Before deploying:** read the instance's connection limit and confirm

```
workers × Σ pool.max_size  +  headroom for migrations/admin/monitoring  ≤  limit
```

[05 §5.5](./05-configuration.md) already validates that the internal limiter can
never admit more work than the pools can hold; this is the outward-facing half of
the same check, and it belongs in the deployment checklist because the plan
tier — not the config file — is the binding constraint.

## 12.5 Do not run the service as `tsdbadmin`

`tsdbadmin` is the instance's administrative role. Running the foundation as it
means any authorization bug, or anyone who reaches the port, gets full control of
the database.

Create a dedicated least-privilege role for the service and grant it only what the
exposed objects need:

```sql
-- run once, by an administrator; owned by the consuming project, not the foundation
CREATE ROLE pgfoundation_app LOGIN PASSWORD :'pw';
GRANT CONNECT ON DATABASE tsdb TO pgfoundation_app;
GRANT USAGE ON SCHEMA public TO pgfoundation_app;
GRANT SELECT ON v_paid_orders TO pgfoundation_app;      -- only what /view may read
GRANT EXECUTE ON FUNCTION fn_orders_by_customer(text) TO pgfoundation_app;
-- no CREATE, no DROP, no superuser
```

This is the control that **survives a bug in the service's own authorization**: if
the login role physically cannot write, an authZ defect degrades to a failed query
rather than data loss. For a read-only data source, set `read_only: true` as well
so the foundation issues `SET default_transaction_read_only = on` — belt and
braces. Reserve `tsdbadmin` for migrations run by the consuming project.

## 12.6 Consequences of a cloud, cross-network target

Round-trip time to a managed cloud database dominates simple query time. Three
parts of the design stop being theoretical:

| Design element | Why it matters here |
|----------------|--------------------|
| **Pipeline mode** in the `sequential` batch strategy ([06 §6.3](./06-concurrency-batch-parallel.md)) | Collapses N round-trips toward 1. When RTT ≫ query time — the normal case across a WAN — this is the single largest win available |
| **`/v1/batch`** ([08](./08-api-surface.md)) | Amortizes TLS handshake, HTTP, auth and validation over N operations |
| **Warm pools** (`min_size ≥ 2`) | Connecting involves a TLS handshake to a remote host; a cold pool pays it on the request path |
| **Prepared statements** ([07 §7.3](./07-performance.md)) | Saves a round-trip per repeated call shape |
| **Streaming** ([ADR-006](./adr/ADR-006-streaming-delivery.md)) | Large results over a WAN must not be materialized |

Timeouts should also be set with the network in mind: `statement_timeout_ms` and
`acquire_timeout_seconds` tuned for LAN latency will produce spurious failures
against a cloud endpoint.

### ✅ Resolved: port 32064 is the DIRECT endpoint

**Confirmed from the service console (2026-08-14): no connection pooler is
configured** — the Overview offers "+ Connection pooler" as a feature to add, so
`32064` is the primary session endpoint. Everything below therefore *works*; it is
retained because the answer changes the day somebody enables a pooler, and the
consequences are not obvious from the config diff that would introduce it.

Managed providers commonly offer both a direct (session-mode) endpoint and a
**transaction-mode connection pooler**. A transaction-mode pooler breaks four
things this design relies on:

| Feature | Under a transaction-mode pooler |
|---------|--------------------------------|
| Server-side cursors (`/stream`, `interval`, large reads) | **Break** — the cursor cannot outlive the transaction it was declared in |
| `LISTEN/NOTIFY` (`/subscribe`) | **Breaks** — session-scoped |
| Session `SET` (`statement_timeout`, `default_transaction_read_only`) | **Leaks or is lost** between clients |
| Prepared statements | Degraded or unavailable, depending on pooler protocol support |

The foundation **is itself a connection concentrator**, so stacking it behind a
transaction pooler is largely redundant and actively costs features. The current
setup is already correct: **the direct endpoint, with the foundation owning
pooling.** If a pooler is ever added, it must be added as a *second* endpoint for
callers that want it — not swapped in underneath this service.

## 12.7 This target is TimescaleDB

Timescale is PostgreSQL, so **everything in this plan works against it unchanged** —
that is the point of `date_bin`-based interval compilation
([06](./06-concurrency-batch-parallel.md), inherited doc 16). Nothing is blocked.

What changes is that the **deferred time-series capability now has a concrete
target** rather than a hypothetical one ([01 §1.4](./01-scope-and-parity.md)):

| Available on this instance | Foundation's position today |
|---------------------------|----------------------------|
| Hypertables | Consumer-owned. The foundation creates no tables and no hypertables |
| `time_bucket`, `time_bucket_gapfill` | The **interval-bucketing Strategy seam** exists for exactly this. The foundation ships the generic `date_bin` implementation; a Timescale Strategy would plug in behind it with **no core change** and no caller change |
| Continuous aggregates | Refreshable through the plain-SQL execution API; scheduling and ownership belong to the consumer |
| Compression, retention policies | Out of scope — the consuming project's operational concern |

**Recommendation:** keep the generic `date_bin` Strategy for P0–P5 and measure. If
interval queries over hypertables become a hot path, implementing the `time_bucket`
Strategy is an additive change behind an existing seam — which is precisely the
outcome the seam was designed to allow. Do not pull time-series scope into the
foundation on the strength of the target alone.

## 12.8 What does not change

- **The foundation still owns no tables and runs no migrations.** The `tsdb`
  schema — including any hypertables — is defined and owned by the consuming
  project.
- **No Timescale-specific dependency** enters `requirements.txt`.
- **No provider name is hard-coded.** Host, port, database and role are config;
  moving to a different instance or provider is a config change.

## 12.9 Deployment checklist

1. [ ] Least-privilege login role created (**not** `tsdbadmin`) — §12.5
2. [ ] Password stored in the platform secret manager; `PGF_TSDB_DSN` injected at runtime — §12.2
3. [ ] `sslmode=verify-full` with the CA bundle reachable — §12.3
4. [ ] Direct vs pooled endpoint confirmed for port `32064` — §12.6
5. [ ] `workers × Σ pool.max_size + headroom ≤ instance connection limit` — §12.4
6. [ ] Timeouts sized for WAN latency, not LAN — §12.6
7. [ ] `auth.enabled: true` and `enable_raw_sql: false` in the prod profile — [05 §5.6](./05-configuration.md)
8. [ ] `GET /ready` wired to the platform's readiness probe (unversioned) — [09 §9.1b](./09-project-structure.md)
9. [ ] `application_name` carries the request id, so `pg_stat_activity` ties back to callers — [11 §11.7](./11-observability-instrumentation-points.md)

## 12.10 Runbook — adding a data source

Managing N databases from one process is the foundation's core job
([G1 / Requirement 1](./01-scope-and-parity.md)), so **a new database does not
mean a new deployment.** But "new database" has three levels, and the cheapest one
that meets the isolation requirement is usually the right one.

| | New **schema** in `tsdb` | New **database**, same service | New **Timescale service** |
|---|---|---|---|
| Provider change | none | `CREATE DATABASE` | provision an instance |
| Foundation change | **none at all** | one `datasources:` entry | one `datasources:` entry |
| New connection pool | no — reuses `tsdb` | yes | yes |
| Connection budget | free | **same** instance limit | own limit |
| SQL-joinable with existing data | **yes** | **no** | **no** |
| Resource / blast-radius isolation | shared | shared CPU, RAM, disk | isolated |
| Cost | none | none | another instance |

### The deciding question

**Does the new data need to be joined with the existing data in SQL?**
PostgreSQL cannot query across databases without `dblink` / `postgres_fdw`, so:

- **Needs joining** → use a **schema** in `tsdb`. No config change whatsoever —
  `/view`, `/function` and `/procedure` already accept a `"schema"` field
  ([08 §8.2](./08-api-surface.md)).
- **Independent data** → a separate **database** is fine, and it buys a real
  bulkhead: its own pool and its own circuit breaker, so it cannot starve `tsdb`.
  Fetch from both in a single call with `POST /v1/batch` in `parallel` mode —
  cross-datasource fan-out is exactly what that strategy is for
  ([06 §6.3](./06-concurrency-batch-parallel.md)).
- **Needs its own CPU/RAM, region, plan tier, or compliance boundary** → a new
  **service**.

### The change itself

```yaml
datasources:
  - name: tsdb
    dsn: ${env:PGF_TSDB_DSN}
  - name: analytics                 # ← the entire change
    dsn: ${env:PGF_ANALYTICS_DSN}
    role: replica
    read_only: true
    pool: { min_size: 1, max_size: 8 }
```

Config only. The source is then immediately reachable at
`/v1/datasources/analytics/…`, is batch-dispatchable, and appears in
`GET /v1/datasources` and in health.

### Two things to check before doing it

1. **It requires a restart.** The registry is built once at startup and **hot
   reload is deferred** ([05 §5.7](./05-configuration.md)) — a rolling restart
   across replicas, so no downtime, but it is not live-applied. Known deferred
   item, not an oversight.
2. **Connection budget.** A new source on the *same* instance draws from the same
   limit: 4 workers × `max_size: 8` is **32** more connections (§12.4). Being near
   the tier cap is on its own a reason to provision a separate service.

Also confirm with the provider whether `CREATE DATABASE` is permitted on an
existing service, and remember that **extensions are per-database in PostgreSQL** —
a newly created database does not automatically have TimescaleDB enabled.

### Recommendation

Default to a **schema in `tsdb`** unless there is a specific reason not to: free,
joinable, no restart. Step up to a separate **database** when the data is genuinely
independent and the pool-level bulkhead is wanted. Provision a new **service** only
for resource or compliance isolation.

In every case the foundation creates nothing — the database, schema and any
hypertables are the consuming project's to define (§12.8).
