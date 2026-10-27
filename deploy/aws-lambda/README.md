# pgfoundation on AWS Lambda + Neon (lowest-cost deployment)

This folder deploys the pgfoundation SQL gateway to **AWS Lambda** behind a public
**Function URL**, with a **Neon free-plan** PostgreSQL database. It is the cheapest
way to run the service on AWS: there is no load balancer, NAT gateway or always-on
compute.

```
client ──HTTPS──► Lambda Function URL ──► Lambda (arm64, 512 MB)
  Authorization: Bearer <key>               AWS Lambda Web Adapter
                                            └─ uvicorn pgfoundation_api (unchanged)
                                                   │ TLS
                                                   ▼
                                            Neon PostgreSQL (direct endpoint)
```

| File | Purpose |
|---|---|
| `Dockerfile` | Service image plus the AWS Lambda Web Adapter 0.9.1; runtime dependencies only |
| `deploy.sh` | Idempotent deployment: ECR repository, image build and push, IAM role, function, Function URL, smoke test |
| `../../postgresqlmodule/code/config/pgfoundation.lambda.yaml` | Gateway configuration for Lambda: `neondb`, `neondb_dev`, `agentruntime` and `agentobservability` datasources, token auth, small pools |

## Cost

| Item | Monthly cost at low traffic |
|---|---|
| Lambda requests and compute | $0 within the always-free tier (1 million requests and 400,000 GB-seconds per month) |
| Function URL | No charge beyond Lambda usage |
| ECR image storage (about 70 MB compressed, 5 newest kept) | A few cents |
| CloudWatch Logs (14-day retention) | $0 within the free tier for light logging |
| Neon free plan | $0 |

## Prerequisites

- AWS CLI v2 with a working profile (default `your-aws-profile`), Docker with Buildx, Git Bash or Linux.
- Credentials in AWS Secrets Manager: secret `zonic-postgresql-server/runtime`
  (account `633160612720`, `us-east-1`) holds `PGF_NEON_DSN`, `PGF_NEON_DEV_DSN`,
  `PGF_AGENTRUNTIME_DSN`, `PGF_AGENTOBSERVABILITY_DSN`, `PGF_API_KEY_ZONIC_AGENT_BUILDER` and
  `PGF_API_KEY_ZONIC_AGENT_RUNTIME` and `PGF_API_KEY_ZONIC_AGENT_RUNTIME_NODE` as one JSON object. `deploy.sh` reads it first and adds any
  value it does not hold yet (from `.env` / `.env.lambda`), keeping the others.
  Read the values with
  `aws secretsmanager get-secret-value --secret-id zonic-postgresql-server/runtime --region us-east-1`.
- Fallback when the secret does not hold a value: Neon **direct** connection strings
  (hosts without `-pooler`), saved in the gitignored repository-root `.env`; the
  script then stores them in the secret:

  ```
  PGF_NEON_DSN=postgresql://<user>:<password>@<endpoint>.<region>.aws.neon.tech/neondb?sslmode=require
  PGF_NEON_DEV_DSN=postgresql://<user>:<password>@<dev-endpoint>.<region>.aws.neon.tech/neondb?sslmode=require
  PGF_AGENTRUNTIME_DSN=postgresql://<user>:<password>@<runtime-endpoint>.<region>.aws.neon.tech/neondb?sslmode=require
  PGF_AGENTOBSERVABILITY_DSN=...   # optional; defaults to PGF_AGENTRUNTIME_DSN
  ```

  | Datasource | Neon branch | Use |
  |---|---|---|
  | `neondb` | `production` | Demonstrations and real workflows |
  | `neondb_dev` | `dev` | Development, the builder's live test suite, and the Node runtime (`zonic-agent-runtime-node`: `agent_builder`, `agent_runtime`, `agent_observability`) |
  | `agentruntime`, `agentobservability` | `production` of project `agent-build-langgraph` | The Agent Runtime's schemas (`agent_runtime`, `agent_observability`, and its copy of `agent_builder`) |

  Both branches use Neon's default database `neondb`. The datasource names are only
  labels in `pgfoundation.lambda.yaml`; each DSN decides which branch is used.
  `deploy.sh` stops if `PGF_NEON_DSN`, `PGF_NEON_DEV_DSN` or `PGF_AGENTRUNTIME_DSN` is missing from both
  the secret and `.env`.

## Deploy or redeploy

```bash
bash deploy/aws-lambda/deploy.sh
```

A caller key missing from both the secret and `.env.lambda` (`PGF_API_KEY_ZONIC_AGENT_BUILDER`,
`PGF_API_KEY_ZONIC_AGENT_RUNTIME`, `PGF_API_KEY_ZONIC_AGENT_RUNTIME_NODE`) is generated and stored in both. Every caller in the policy needs
its key, or the gateway refuses to start. The script prints the endpoint. Later runs build a new
image and roll the function forward. The Lambda region defaults to the Neon
endpoint's region.

## Call the gateway

```bash
curl -s "$URL/health"
curl -s -X POST "$URL/v1/datasources/neondb/function" \
  -H "Authorization: Bearer $KEY" -H "content-type: application/json" \
  -d '{"name":"version","schema":"pg_catalog","scalar":true}'
```

Requests without a valid key receive HTTP 401. API documentation (`/docs`) is disabled.

## Connect the Agent Builder API

Set these for `ZonicAgentBuilderServer_FastAPI` (environment variables or its `.env`):

```
PG_GATEWAY_PROVIDER=http
PG_GATEWAY_BASE_URL=<Function URL without the trailing slash>
PG_GATEWAY_DATASOURCE=neondb_dev        # neondb for production
PG_GATEWAY_SCHEMA=agent_builder
PG_GATEWAY_AUTH_HEADER=Authorization: Bearer <PGF_API_KEY_ZONIC_AGENT_BUILDER>
```

The builder's schema (`agent_builder`: 7 tables, 8 views, 11 functions and
procedures) was installed on both branches on 21 October 2026 with its `db/run_all.sql`
(scripts `001` to `010`). The scripts are idempotent, so rerun them after a change.
Without a local `psql`, use the `postgres:18-alpine` image and pass the DSN as an
environment variable, so it does not appear on the command line:

```bash
export PGURI="$PGF_NEON_DEV_DSN"
MSYS_NO_PATHCONV=1 docker run --rm -e PGURI \
  -v "<path to ZonicAgentBuilderServer_FastAPI>/db:/db:ro" -w /db postgres:18-alpine \
  sh -c 'psql "$PGURI" -v ON_ERROR_STOP=1 -f run_all.sql'
```

## Operating notes

- **Cold starts.** A new Lambda environment starts in about 1 to 2 seconds. After about
  5 idle minutes Neon suspends its compute; the next query waits for it to resume.
- **Stale connections.** The frozen Lambda environment cannot close idle connections
  on time, so the pool validates each connection at checkout
  (`pool.check_on_checkout: true`) and replaces any that Neon has closed.
- **Connection budget.** At most 10 concurrent environments (this account's Lambda
  limit) times `pool.max_size` 3 equals 30 connections per datasource. Each datasource
  is a separate Neon branch with its own compute, so each compute sees at most 30.
- **Limits.** Function timeout 60 seconds; statement timeout 30 seconds.
- **Rotate the caller key.** Generate a new key (`openssl rand -hex 32`), write it to
  `PGF_API_KEY_ZONIC_AGENT_BUILDER` in the secret (`aws secretsmanager put-secret-value`
  with the full JSON object) and in `.env.lambda`, rerun `deploy.sh`, and update every
  caller.
- **Rotate a Neon password.** Reset it in the Neon console, update the DSN in the secret
  and in `.env`, and rerun `deploy.sh`.

## Remove everything

In Git Bash, keep `MSYS_NO_PATHCONV=1` so the log group name is not rewritten into a
Windows path.

```bash
export MSYS_NO_PATHCONV=1
R=us-east-1   # the region the deploy used
aws lambda delete-function-url-config --function-name zonic-postgresql-server --region $R
aws lambda delete-function --function-name zonic-postgresql-server --region $R
aws ecr delete-repository --repository-name zonic-postgresql-server --force --region $R
aws logs delete-log-group --log-group-name /aws/lambda/zonic-postgresql-server --region $R
aws iam detach-role-policy --role-name zonic-postgresql-server-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name zonic-postgresql-server-lambda
```
