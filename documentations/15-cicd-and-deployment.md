# 15 — CI/CD and AWS Deployment Plan

How code gets from a feature branch to a running service on AWS. The database is
already deployed on Tiger Cloud and is **out of scope** ([12](./12-deployment-targets.md));
this document covers the *application* only.

**Deployment shape: an internal microservice.** This service is a *data
foundation* consumed by other services in the platform, not by browsers or
third parties. That single fact decides more of the design than any CI/CD
preference does — see §15.3. Merging to `main` deploys it.

**Status:** plan. Nothing here is implemented yet except the existing CI workflow.

---

## 15.1 What exists today, and what is missing

| Piece | State |
|---|---|
| `.github/workflows/ci.yml` | ✅ lint · types · architecture contracts · tests · optional live integration |
| Branch triggering | ⚠️ **incomplete** — runs on `push: [main]` and on `pull_request`. A push to a feature branch with no PR open runs **nothing** |
| Container image | ❌ no `Dockerfile`, no `.dockerignore` |
| Image registry | ❌ no ECR repository |
| Deployment workflow | ❌ none |
| AWS infrastructure | ❌ none (no VPC/ALB/ECS/IAM defined as code) |
| Secret delivery to AWS | ❌ none |
| Database migrations | ✅ **not applicable** — the foundation owns no schema (§12.8), so there is no migration step to sequence. This removes the single most common source of deployment risk |

---

## 15.2 The two constraints that decide the architecture

Most "deploy a FastAPI app" advice does not apply here, because this service is
a **stateful connection manager**, not a stateless request handler. Two
properties drive every choice below.

### Constraint 1 — the connection budget is a hard, shared limit

Every process holds warm connections open (`min_size: 2`, `max_size: 10` today).
The Tiger Cloud instance allows **200** total and currently uses **22**
([12 §12.4](./12-deployment-targets.md)). The arithmetic that matters:

```
connections held = tasks × uvicorn_workers × Σ(pool.max_size)
```

Autoscaling multiplies this. A platform that scales to 50 instances would
exhaust the database's connection limit long before it ran out of CPU — and the
failure would appear as *the database refusing everyone*, including other
consumers, not as this service degrading. **Any deployment target must have a
hard, enforced ceiling on instance count.**

### Constraint 2 — connections are long-lived by design

- `/stream` returns NDJSON/SSE over a server-side cursor held open for the
  duration of the response.
- `/subscribe/{channel}` holds a `LISTEN` connection open **indefinitely**.
- Warm pools exist precisely so a request does not pay a TLS handshake to a
  remote host.

A platform that freezes, recycles, or time-limits execution breaks all three.

---

## 15.3 Compute: ECS Fargate behind an ALB

| Option | Verdict |
|---|---|
| **ECS Fargate + ALB** | ✅ **Recommended.** Long-running containers keep warm pools; task count is explicitly capped, so the connection budget is enforceable; rolling deploys with health-gated cutover; no servers to patch |
| AWS Lambda | ❌ **Disqualified.** No persistent pool between invocations (each cold start is a new TLS handshake and a new connection), 15-minute ceiling breaks `/subscribe`, and concurrency spikes cause connection storms against a 200-connection limit |
| App Runner | ⚠️ Workable but poor fit: autoscaling is harder to bound tightly, and request-duration limits conflict with open-ended streaming |
| EKS | ⚠️ Correct capabilities, disproportionate operational cost for one service. Revisit if the platform grows to many services |
| EC2 + ASG | ⚠️ Works, but you inherit AMI patching and host management for no gain over Fargate here |

**Region: `us-east-1`**, matching the Tiger Cloud service. Cross-region would add
tens of milliseconds to *every* database round-trip, which is the dominant cost
in this service's latency budget ([07 §7.9](./07-performance.md)).

### Network placement — the microservice decision

```
      ┌──────────────── VPC (us-east-1) ────────────────┐
      │                                                  │
      │  caller services            ┌───────────────┐    │
      │  (agent-workflow-builder,   │ INTERNAL ALB  │    │
      │   insite-platform-api, …) ─►│ (no public IP)│    │
      │                             └───────┬───────┘    │
      │                                     ▼            │
      │                        ┌────────────────────┐    │
      │                        │ ECS Fargate tasks  │    │
      │                        │ private subnets    │    │
      │                        └─────────┬──────────┘    │
      │                                  │ NAT Gateway   │
      └──────────────────────────────────┼───────────────┘
                                         ▼
                        Tiger Cloud  aqy26lurzx…:32064  (public internet, TLS)
```

Four consequences, each of which is a real decision rather than a default:

1. **The load balancer is `internal`, not `internet-facing`.** Nothing about
   this API belongs on the public internet: it exposes raw data access, and its
   authorization model is *caller-level*, not end-user-level ([ADR-008](./adr/ADR-008-auth-boundary.md)).
   An internal scheme means the service has no public DNS and no public IP at
   all — the strongest control available, and it costs nothing.
2. **Callers reach it by a stable internal name**, via ECS Service Connect (or
   Cloud Map): `http://pgfoundation.internal:8000`. Callers must never carry an
   IP or a task address.
3. **The security group is the real perimeter.** Ingress on `:8000` is allowed
   **only** from the security groups of named caller services — not from a CIDR
   block, which would silently include anything later placed in that range.
4. **Egress still needs a route to the public internet**, because Tiger Cloud is
   a public endpoint. Private subnets therefore need a **NAT Gateway** (~$32/mo,
   §15.14). A VPC endpoint cannot help here — those reach AWS services, and
   Timescale is not one. The alternative, public subnets with public IPs and a
   locked-down security group, saves the NAT cost but puts the tasks themselves
   on public address space; for a data-access service, the NAT is worth it.

### Sizing to start

| Setting | Value | Why |
|---|---|---|
| Task CPU / memory | 0.5 vCPU / 1 GB | The service is I/O-bound, not CPU-bound |
| uvicorn workers per task | **1** | Keeps the connection formula `tasks × pool.max_size`. Scale by task count, not workers — one variable instead of two |
| Desired tasks | 2 | Survives one AZ failure |
| **Max tasks (hard cap)** | **6** | 6 × 10 = **60 connections**, 30% of the instance limit, leaving headroom for other consumers and for `psql` |

---

## 15.4 Pipeline topology

```
feature branch push ─────► CI (lint · types · contracts · tests)          ~2 min
                              │
open / update PR ─────────► CI + build image (validate Dockerfile, no push)
                              │
                     merge to main
                              │
                              ▼
                    build + push image to ECR (tag = git SHA)
                              │
                              ▼
                    deploy → STAGING          (automatic)
                              │
                              ▼
                    smoke tests against the internal ALB
                              │
                              ▼
                    deploy → PRODUCTION       (automatic, rolling, health-gated)
                              │
                              ▼
                    post-deploy verification + auto-rollback on failure
```

**Merging to `main` deploys to AWS with no further clicks.** Both requirements
are met directly: any branch push runs CI, and a merge runs the whole deployment
chain through to production.

Three things make an automatic production deploy defensible rather than reckless
here, and all three are gates in the pipeline — not good intentions:

| Guard | Effect |
|---|---|
| **Staging must go green first** | `deploy-production` has `needs: [deploy-staging]`. A failed staging deploy or smoke test stops the chain before production is touched |
| **ECS deployment circuit breaker** | New tasks that fail health checks are rolled back automatically by ECS, without a human noticing first (§15.12) |
| **Health-gated rolling replacement** | `minimumHealthyPercent: 100` — old tasks are only drained once new ones pass `/ready`. A broken image never removes serving capacity |

> **Turning the manual gate back on is one setting**, not a code change: add
> required reviewers to the `production` GitHub Environment. Worth doing once
> real callers depend on the service, and worth skipping while you are the only
> consumer. That trade is yours to re-make later; nothing in the workflow files
> changes either way.

**Manual deploys and rollbacks** are available without a merge via
`workflow_dispatch` with an image tag input — the same job, a different trigger.
That is the "deploy a specific SHA" and "roll back now" path (§15.12).

### Images are immutable and tagged by commit

Tag with the **git SHA**, never `latest`. The image that passed staging is
bit-for-bit the image that reaches production, and a rollback is "point the
service at the previous SHA" rather than "rebuild and hope".

---

## 15.5 Workflow 1 — CI on every branch

The change to the existing file is small:

```yaml
on:
  push:
    branches: ['**']        # every branch, not just main
  pull_request:
```

Plus a concurrency group, so pushing three times in a minute does not run three
full pipelines:

```yaml
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
```

> **Note on the `integration` job.** It is currently gated on
> `github.event_name == 'push'`, which — once pushes to every branch trigger CI —
> would run live-database tests on every feature branch. Those tests `CREATE
> TABLE`. Re-gate it to `github.ref == 'refs/heads/main'` and point
> `PGF_TEST_DSN` at a **dedicated test database**, never at `tsdb`.

## 15.6 Workflow 2 — build and publish

Runs only on `main`. Authenticates to AWS by **OIDC**, not stored keys:

```yaml
permissions:
  id-token: write          # required for OIDC
  contents: read

steps:
  - uses: actions/checkout@v4
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: arn:aws:iam::<ACCOUNT>:role/github-actions-pgfoundation
      aws-region: us-east-1
  - uses: aws-actions/amazon-ecr-login@v2
  - run: |
      IMAGE=$ECR_REGISTRY/pgfoundation:${{ github.sha }}
      docker build -t $IMAGE postgresqlmodule/code
      docker push $IMAGE
```

**No AWS access keys in GitHub secrets.** A GitHub OIDC identity provider in IAM
issues short-lived credentials to a role whose trust policy is restricted to this
repository — ideally to `ref:refs/heads/main`. A leaked GitHub secret then grants
nothing, because there is no long-lived credential to leak.

## 15.7 Workflow 3 — deploy

```yaml
on:
  push:
    branches: [main]                 # merge to main = deploy
  workflow_dispatch:                 # manual deploy / rollback of any SHA
    inputs:
      image_tag:
        description: Image tag (git SHA) to deploy
        required: true

jobs:
  deploy-staging:
    environment: staging
    steps:
      - render task definition with image = ${{ inputs.image_tag || github.sha }}
      - aws ecs update-service --force-new-deployment
      - aws ecs wait services-stable          # fails the job if not stable
      - run smoke tests against the internal ALB

  deploy-production:
    needs: [deploy-staging]                   # ← the gate that matters
    environment: production
    steps: (identical, prod cluster)
```

`aws ecs wait services-stable` is what makes this safe: it returns non-zero if
the new tasks never reach a steady state, so a bad image fails the job instead of
quietly leaving a half-deployed service. Combined with `needs:`, production is
never touched by a revision that could not stabilise in staging.

> **Smoke tests run from inside the VPC.** The ALB is internal, so a GitHub-hosted
> runner cannot reach it. Options: run the smoke step as a one-off ECS task in the
> same VPC (simplest, no extra infrastructure), or use a self-hosted runner. This
> is a direct consequence of §15.3 and is easy to miss until the first deploy.

---

## 15.8 The container image

```dockerfile
# --- build stage -----------------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- runtime stage ---------------------------------------------------------
FROM python:3.12-slim
COPY --from=build /install /usr/local
WORKDIR /app
COPY pgfoundation/     ./pgfoundation/
COPY pgfoundation_api/ ./pgfoundation_api/
COPY config/           ./config/

RUN useradd --system --uid 10001 pgf
USER pgf                                   # never root

EXPOSE 8000
CMD ["uvicorn", "pgfoundation_api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

Points that are specific to this project:

- **`config/` ships in the image; secrets do not.** Config files contain only
  `${env:...}` references ([05 §5.2](./05-configuration.md)), so the image holds
  structure and tuning, while the DSN arrives at runtime (§15.9).
- **The plain `uvicorn` CLI is correct here.** `run_local.py` exists because
  psycopg's async driver requires a `SelectorEventLoop` on **Windows**; Linux
  containers use `epoll` and are unaffected. `run_local.py` is a developer tool
  and must not be the container entrypoint.
- **Test the build in CI on PRs.** A Dockerfile that only ever builds during a
  deploy is a Dockerfile that breaks during a deploy.
- **`.dockerignore` must exclude `.env`**, `tests/`, `venv314/`, `__pycache__`
  and `documentations/`. Excluding `.env` is not an optimisation — it is what
  stops a real credential being baked into a layer.

---

## 15.9 Secrets

```
AWS Secrets Manager: /pgfoundation/prod/tsdb-dsn
        │  (referenced by ARN, not value, in the task definition)
        ▼
ECS task definition  →  "secrets": [{ "name": "PGF_TSDB_DSN", "valueFrom": "<arn>" }]
        ▼
container environment  →  config resolves ${env:PGF_TSDB_DSN} at startup
```

The value is injected by the ECS agent at task start. It never appears in the
image, in the task definition JSON, in GitHub, or in CloudFormation output. The
execution role gets `secretsmanager:GetSecretValue` on **that one ARN**.

This is the point at which to stop using `tsdbadmin`. [12 §12.5](./12-deployment-targets.md)
recommends a least-privilege role; a deployment that reads its DSN from a secret
store is exactly when that becomes free to do — you are already editing the
credential.

---

## 15.10 Load balancer settings this application actually needs

Defaults will appear to work and then fail in ways that are hard to attribute.

| Setting | Default | Required | Why |
|---|---|---|---|
| Idle timeout | 60 s | **300 s+** | An NDJSON stream or SSE subscription with a gap longer than the idle timeout is **silently cut by the ALB**. The client sees a truncated stream, not an error |
| Health check path | `/` | **`/ready`** | `/ready` reflects data-source health; `/health` is the liveness signal. Using `/` would report healthy while every query fails |
| Health check codes | 200 | 200 | `/ready` returns non-200 when degraded — that is the behaviour to rely on |
| Deregistration delay | 300 s | **≥ ECS `stopTimeout`** | Must exceed the graceful drain window, or in-flight streams are killed at deploy time |
| ECS `stopTimeout` | 30 s | **120 s** | SIGTERM starts the drain; the service needs time to finish in-flight work before SIGKILL |

> **`/subscribe` needs an application change before it survives an ALB.** A
> `LISTEN` channel that is quiet for longer than the idle timeout produces no
> bytes, so the ALB closes an otherwise healthy connection. The fix is a periodic
> SSE heartbeat comment (`: keepalive\n\n`) on an interval shorter than the idle
> timeout. This is not currently implemented — see §15.13, P-CD5.

---

## 15.11 Prerequisites for the production profile — read before scheduling

`config/profiles/prod.yaml` asserts `auth.enabled: true` with `provider: mtls`.
Two of its requirements are **not satisfiable by the plan above as written**, and
both were verified in code rather than assumed.

### (a) The `mtls` provider is unsafe behind a plain ALB

`MeshIdentityAuthenticator` trusts an HTTP header — `x-client-cert-subject` by
default. That is sound **only** when a mesh or ingress sets the header from a
verified client certificate *and strips any client-supplied copy at the edge*.
An Application Load Balancer does neither by default. Deployed as described, any
caller could send:

```
-H 'x-client-cert-subject: orders-api'
```

and be authenticated as `orders-api`. The project's own documentation states
this ("impersonation is a one-line curl"); it is repeated here because the AWS
topology is exactly where the assumption silently stops holding.

Three ways out, in increasing order of effort:

1. **Use `provider: token`** for the first deployment — already implemented and
   tested, with per-caller keys from Secrets Manager. **Recommended.**
2. **ALB mutual TLS**, which populates `x-amzn-mtls-clientcert`, plus a small
   adapter mapping that header to a subject — and an explicit rule that strips
   any inbound `x-client-cert-subject`.
3. A service mesh (App Mesh / Istio) if the platform grows into one.

**Being internal narrows the blast radius; it does not fix this.** An internal
ALB means the attacker must already be inside the VPC — but "inside the VPC" is
exactly where every other microservice lives. A header-trusting check treats a
compromised sibling service, or any workload that can reach port 8000, as fully
authenticated. The security-group rule in §15.3 is the control doing the real
work; the `mtls` provider as configured would be adding a check that any caller
can satisfy.

### (b) `policy.callers` must be populated — and this is the microservice step

Config validation refuses `auth.enabled: true` with `policy.default: deny` and
an empty caller list — "that authorizes nobody, which is a misconfiguration, not
a lockdown". Each consuming service needs an entry:

```yaml
auth:
  enabled: true
  provider: token
  policy:
    default: deny
    callers:
      - subject: agent-workflow-builder-api
        datasources: [tsdb]
        actions: [read]
      - subject: insite-platform-api
        datasources: [tsdb]
        actions: [read, write]
```

The file names *who may call*; the matching keys live in Secrets Manager and are
injected as `api_keys` (§15.9) — the config never holds the proof, only the
policy.

> **This is the gate [14 §14.5](./14-build-status.md) has been holding open.**
> The standing constraint is: *do not onboard a second consuming API until the
> deployment carries real caller identity.* Deploying as a shared microservice is
> precisely the moment that constraint binds. One consumer on a trusted network
> was defensible; several consumers with no caller identity is not — the service
> could no longer tell which of them issued a given query, which matters as much
> for attribution and rate-limiting as for security.

### (c) `observability.provider: otel` has no adapter yet

Only the metrics adapter exists; the OTel binding belongs to the external
observability project (ADR-014, restated in
[11 §11.1](./11-observability-instrumentation-points.md)). This is not a startup
blocker — `build_metrics` falls back to the Null Object — but
prod will emit no traces, and the profile currently implies otherwise. Either
bind an adapter or set the field honestly.

**Net effect:** staging can be deployed as soon as the pipeline exists.
Production additionally requires an auth decision (a), a caller policy (b), and
ideally the least-privilege database role from §12.5.

---

## 15.12 Rollback

```bash
aws ecs update-service --cluster prod --service pgfoundation \
    --task-definition pgfoundation:<PREVIOUS_REVISION>
```

Because images are SHA-tagged and immutable, the previous task definition
revision still references a real image. Rollback is a control-plane operation of
about a minute, with no rebuild. Keep the last ~10 revisions.

A **circuit breaker** on the ECS deployment (`deploymentCircuitBreaker: {enable:
true, rollback: true}`) makes this automatic when new tasks fail health checks —
worth enabling from day one.

---

## 15.13 Delivery phases

| Phase | Work | Done when |
|---|---|---|
| **P-CD1** | Fix CI triggers (all branches, concurrency group, re-gate the integration job) | A push to a feature branch runs CI; integration tests do not run against `tsdb` |
| **P-CD2** | `Dockerfile` + `.dockerignore`; build in CI on PRs; run the container locally against Tiger Cloud | `docker run` serves `/health` = healthy; image has no `.env` layer; runs as non-root |
| **P-CD3** | AWS foundations: ECR repo, VPC/private subnets, **internal** ALB, NAT, ECS cluster, task + execution roles, GitHub OIDC provider and role | `aws sts get-caller-identity` succeeds from Actions with no stored keys; a task in the private subnet reaches Tiger Cloud |
| **P-CD4** | Secrets Manager entry; staging service; deploy workflow; in-VPC smoke-test runner | Merge to `main` lands automatically on staging; `/ready` green |
| **P-CD4b** | **Microservice wiring**: Service Connect name, caller security-group rules, per-caller keys and `policy.callers` | A named caller reaches the service by internal DNS; an unnamed one is refused at the security group *and* by policy |
| **P-CD5** | Streaming hardening: SSE heartbeat, ALB idle timeout, deregistration delay, `stopTimeout`, graceful drain verified | A 10-minute idle `/subscribe` stays open; a deploy during an active stream does not truncate it |
| **P-CD6** | Production: auth decision (§15.11a), caller policy, least-privilege DB role, deployment circuit breaker | A merge to `main` reaches prod unattended; a deliberately broken image auto-rolls-back without human action |

P-CD1 and P-CD2 are worth doing regardless of the AWS decisions — they are
useful on their own and unblock everything else.

---

## 15.14 Rough monthly cost (us-east-1, staging + prod)

| Item | Estimate |
|---|---|
| Fargate, 2 tasks × 0.5 vCPU / 1 GB, always on | ~$36 |
| Second environment (staging, 1 task) | ~$18 |
| Internal ALB (2) | ~$36 |
| **NAT Gateway** (§15.3 — egress to Tiger Cloud) | ~$32 + $0.045/GB |
| ECR storage + data transfer | ~$5 |
| Secrets Manager | ~$1 |
| **Total** | **≈ $130–140 / month** |

Two levers if that is too high for a first deployment:

* **Share one NAT Gateway** between staging and prod (they are in the same VPC
  anyway) — already assumed above; a second would add ~$32.
* **Public subnets instead of NAT** saves the ~$32 but places tasks on public
  address space. Acceptable only with a strict security group, and it weakens
  the §15.3 posture. Revisit if cost pressure is real; do not start there.

Database egress volume is worth watching: every result set crosses the NAT and
is billed per GB. A `SELECT *` habit is now a line item, which is one more
reason the governed `columns` field exists.

---

## 15.15 Decisions needed from you

1. **Which services will call it, by name.** This drives three things at once:
   `policy.callers`, the security-group ingress rules, and the per-caller keys in
   Secrets Manager. Your Postman workspace suggests `agent-workflow-builder-api`,
   `insite-platform-api`, `redis-cache-api` and an MCP service — confirm the real
   list rather than let me guess it.
2. **Does the platform already have a VPC?** A shared VPC changes §15.3 from
   "build a network" to "add a service and two security-group rules", and lets
   callers reach it without any cross-VPC work. If other services already run on
   ECS, this service should join that cluster.
3. **AWS account and region** — confirm `us-east-1`, matching the database.
4. **Auth provider for production** — `token` (implemented, recommended) or ALB
   mTLS (stronger, more work). §15.11a.
5. **Infrastructure as code** — Terraform, CDK, or CloudFormation? Click-ops for
   the first pass is acceptable but should not survive P-CD6.
6. **Staging environment** — keep it (recommended: it is the gate that makes the
   automatic production deploy safe) or collapse to one environment and accept a
   direct-to-prod merge?
7. **Test database** — the integration job needs a database it may `CREATE
   TABLE` in. A separate Tiger Cloud database (§12.10) rather than `tsdb`.

---

## 15.16 What changes if this is one of several microservices

Worth stating explicitly, because most of it is *not* this repository's work:

| Concern | Owner |
|---|---|
| Caller identity and per-caller keys | **This service** — `policy.callers`, §15.11b |
| End-user identity, tenancy, row-level rules | **Calling services** — deliberately out of scope ([ADR-008](./adr/ADR-008-auth-boundary.md)) |
| Shared VPC, cluster, DNS namespace | Platform |
| The connection budget | **This service, on behalf of everyone** — §15.2. It is the only component that can enforce it, because it is the only one that opens connections |

That last row is the reason this service should stay the *sole* path to the
database. If callers also connect directly, the ceiling in §15.2 becomes a
suggestion, and the failure mode it prevents — the database refusing every
consumer at once — comes back with no single place to fix it.
