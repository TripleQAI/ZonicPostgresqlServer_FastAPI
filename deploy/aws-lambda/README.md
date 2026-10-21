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
| `../../postgresqlmodule/code/config/pgfoundation.lambda.yaml` | Gateway configuration for Lambda: `neondb` datasource, token auth, small pool |

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
- A Neon **direct** connection string (host without `-pooler`), saved in the gitignored
  repository-root `.env`:

  ```
  PGF_NEON_DSN=postgresql://<user>:<password>@<endpoint>.<region>.aws.neon.tech/neondb?sslmode=require
  ```

  The datasource alias is `neondb`, Neon's default database name. If the database has
  a different name, change `name:` in `pgfoundation.lambda.yaml`.

## Deploy or redeploy

```bash
bash deploy/aws-lambda/deploy.sh
```

The first run creates the caller key in the gitignored `.env.lambda`
(`PGF_API_KEY_ZONIC_AGENT_BUILDER`) and prints the endpoint. Later runs build a new
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
PG_GATEWAY_DATASOURCE=neondb
PG_GATEWAY_SCHEMA=agent_builder
PG_GATEWAY_AUTH_HEADER=Authorization: Bearer <PGF_API_KEY_ZONIC_AGENT_BUILDER>
```

The builder's views and procedures must exist in Neon first: run its `db/` scripts
(`000_preflight.sql`, then `001` to `010`) against the Neon database.

## Operating notes

- **Cold starts.** A new Lambda environment starts in about 1 to 2 seconds. After about
  5 idle minutes Neon suspends its compute; the next query waits for it to resume.
- **Stale connections.** The frozen Lambda environment cannot close idle connections
  on time, so the pool validates each connection at checkout
  (`pool.check_on_checkout: true`) and replaces any that Neon has closed.
- **Connection budget.** At most 10 concurrent environments (this account's Lambda
  limit) times `pool.max_size` 3 equals 30 connections.
- **Limits.** Function timeout 60 seconds; statement timeout 30 seconds.
- **Rotate the caller key.** Delete the line from `.env.lambda`, rerun `deploy.sh`, and
  update every caller.

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
