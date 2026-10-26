#!/usr/bin/env bash
# Deploy pgfoundation to AWS Lambda behind a public Function URL.
#
# Cheapest AWS shape for this service: Lambda (arm64 container image) runs the
# unchanged uvicorn app through the AWS Lambda Web Adapter, and a Function URL
# exposes it over HTTPS. No load balancer, no NAT gateway, no always-on compute:
# at low traffic, Lambda and Function URL usage stay inside the always-free tier,
# and the only standing cost is ECR storage for the image (cents per month).
#
# Idempotent: the first run creates everything; later runs build, push and roll
# the function forward to the new image and settings.
#
# Usage (from the repository root, in Git Bash or Linux):
#   bash deploy/aws-lambda/deploy.sh
#
# Inputs, read from AWS Secrets Manager first, then from local files:
#   Secret zonic-postgresql-server/runtime (JSON, account and region of the function):
#     PGF_NEON_DSN                      Neon DIRECT connection string (production branch)
#     PGF_NEON_DEV_DSN                  Neon DIRECT connection string (branch dev)
#     PGF_AGENTRUNTIME_DSN              Agent Runtime database (datasource agentruntime)
#     PGF_AGENTOBSERVABILITY_DSN        datasource agentobservability; defaults to PGF_AGENTRUNTIME_DSN
#     PGF_AGENTBUILDER_DSN              datasource agentbuilder (the builder's workflows); defaults to PGF_AGENTRUNTIME_DSN
#     PGF_API_KEY_ZONIC_AGENT_BUILDER   caller key
#     PGF_API_KEY_ZONIC_AGENT_RUNTIME   caller key
#   Fallback files (both gitignored by `.env` / `.env.*`):
#     .env          the DSNs
#     .env.lambda   the caller keys; a missing key is generated
#   The secret is created when missing and updated with any value it does not hold yet;
#   keys it holds that this script does not use are kept.
#   Every caller in config/pgfoundation.lambda.yaml needs its key, or the gateway refuses to start.
#   Read it with: aws secretsmanager get-secret-value --secret-id zonic-postgresql-server/runtime
#
# Overridable environment variables:
#   AWS_PROFILE    (default your-aws-profile)
#   AWS_REGION     (default: the Neon endpoint's region, else us-east-1)
#   SECRET_ID      (default zonic-postgresql-server/runtime)
#   SECRET_REGION  (default: AWS_REGION, else us-east-1)
#   FUNCTION_NAME, ECR_REPO (default zonic-postgresql-server)
#   ROLE_NAME      (default zonic-postgresql-server-lambda)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-your-aws-profile}"
FUNCTION="${FUNCTION_NAME:-zonic-postgresql-server}"
REPO="${ECR_REPO:-zonic-postgresql-server}"
ROLE="${ROLE_NAME:-zonic-postgresql-server-lambda}"
ARCH=arm64
MEMORY_MB=512
TIMEOUT_S=60
LOG_RETENTION_DAYS=14
API_KEY_VAR=PGF_API_KEY_ZONIC_AGENT_BUILDER
RUNTIME_KEY_VAR=PGF_API_KEY_ZONIC_AGENT_RUNTIME
SECRET_ID="${SECRET_ID:-zonic-postgresql-server/runtime}"
SECRET_REGION="${SECRET_REGION:-${AWS_REGION:-us-east-1}}"
PY="$(command -v python3 || command -v python)"

log() { printf '\n==> %s\n' "$*"; }

# Read NAME=value from a dotenv file, without quotes or carriage returns.
# A missing variable reads as empty (grep's "no match" would otherwise end the script
# under pipefail, silently, before an optional variable could fall back to its default).
read_var() {
  [ -f "$1" ] || return 0
  { grep -E "^$2=" "$1" || true; } | tail -1 | cut -d= -f2- | tr -d '\r' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Windows AWS CLI needs a Windows path inside file:// URIs.
native_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }

# --- secrets -----------------------------------------------------------------
# Secrets Manager is the source of truth; the gitignored dotenv files are the fallback.
# Values stay in variables and a temp file; they are never printed or put on a command line.
SECRET_JSON="$(aws --region "$SECRET_REGION" secretsmanager get-secret-value --secret-id "$SECRET_ID" \
  --query SecretString --output text 2>/dev/null || true)"
secret_var() {
  [ -n "$SECRET_JSON" ] || return 0
  SECRET_JSON="$SECRET_JSON" "$PY" -c \
    'import json, os, sys; print(json.loads(os.environ["SECRET_JSON"]).get(sys.argv[1], ""))' "$1" | tr -d '\r'
}
# Value of $2: the secret first, then the dotenv file $1.
get_var() {
  local value
  value="$(secret_var "$2")"
  if [ -n "$value" ]; then printf '%s' "$value"; else read_var "$1" "$2"; fi
}
if [ -n "$SECRET_JSON" ]; then
  log "credentials from Secrets Manager ($SECRET_ID, $SECRET_REGION)"
else
  log "secret $SECRET_ID not found; credentials from .env and .env.lambda"
fi

NEON_DSN="$(get_var "$ROOT/.env" PGF_NEON_DSN)"
if [ -z "$NEON_DSN" ]; then
  echo "PGF_NEON_DSN is missing from secret $SECRET_ID and from $ROOT/.env" >&2
  exit 1
fi
# Datasource neondb_dev (config/pgfoundation.lambda.yaml) reads this one.
NEON_DEV_DSN="$(get_var "$ROOT/.env" PGF_NEON_DEV_DSN)"
if [ -z "$NEON_DEV_DSN" ]; then
  echo "PGF_NEON_DEV_DSN is missing from secret $SECRET_ID and from $ROOT/.env (Neon branch dev, datasource neondb_dev)" >&2
  exit 1
fi
# Datasources agentruntime and agentobservability (the Agent Runtime's database).
RUNTIME_DSN="$(get_var "$ROOT/.env" PGF_AGENTRUNTIME_DSN)"
if [ -z "$RUNTIME_DSN" ]; then
  echo "PGF_AGENTRUNTIME_DSN is missing from secret $SECRET_ID and from $ROOT/.env (datasource agentruntime)" >&2
  exit 1
fi
OBSERVABILITY_DSN="$(get_var "$ROOT/.env" PGF_AGENTOBSERVABILITY_DSN)"
OBSERVABILITY_DSN="${OBSERVABILITY_DSN:-$RUNTIME_DSN}"
# Datasource agentbuilder: the builder's workflows, in the same database by default.
BUILDER_DSN="$(get_var "$ROOT/.env" PGF_AGENTBUILDER_DSN)"
BUILDER_DSN="${BUILDER_DSN:-$RUNTIME_DSN}"
for dsn in "$NEON_DSN" "$NEON_DEV_DSN" "$RUNTIME_DSN" "$OBSERVABILITY_DSN" "$BUILDER_DSN"; do
  case "$dsn" in
    *-pooler.*) echo "warning: a DSN uses Neon's pooled endpoint; the direct endpoint is recommended" >&2 ;;
  esac
done

# Same region as the database keeps every query off the long-haul network.
NEON_REGION="$(printf '%s' "$NEON_DSN" | sed -nE 's/.*\.([a-z]{2}-[a-z]+-[0-9])\.aws\.neon\.tech.*/\1/p')"
REGION="${AWS_REGION:-${NEON_REGION:-us-east-1}}"
aws_() { aws --region "$REGION" "$@"; }
# Git Bash rewrites arguments that start with "/" (such as log group names) into
# Windows paths before a Windows program sees them. Docker needs that rewriting
# for local paths, so it is switched off only for these calls. No effect on Linux.
aws_raw() { MSYS_NO_PATHCONV=1 aws --region "$REGION" "$@"; }

# A caller key from the secret or .env.lambda; generated (and kept in .env.lambda) when missing.
caller_key() {
  local value
  value="$(get_var "$ROOT/.env.lambda" "$1")"
  if [ -z "$value" ]; then
    value="$(openssl rand -hex 32)"
    printf '%s=%s\n' "$1" "$value" >> "$ROOT/.env.lambda"
    log "generated a new caller key in .env.lambda ($1)" >&2
  fi
  printf '%s' "$value"
}
API_KEY="$(caller_key "$API_KEY_VAR")"
RUNTIME_KEY="$(caller_key "$RUNTIME_KEY_VAR")"

# Every value the function receives, as one JSON object (also the secret's content).
export NEON_DSN NEON_DEV_DSN RUNTIME_DSN OBSERVABILITY_DSN BUILDER_DSN API_KEY API_KEY_VAR RUNTIME_KEY RUNTIME_KEY_VAR
VALUES_JSON="$("$PY" -c 'import json, os; e = os.environ; print(json.dumps({
  "PGF_NEON_DSN": e["NEON_DSN"], "PGF_NEON_DEV_DSN": e["NEON_DEV_DSN"],
  "PGF_AGENTRUNTIME_DSN": e["RUNTIME_DSN"], "PGF_AGENTOBSERVABILITY_DSN": e["OBSERVABILITY_DSN"],
  "PGF_AGENTBUILDER_DSN": e["BUILDER_DSN"],
  e["API_KEY_VAR"]: e["API_KEY"], e["RUNTIME_KEY_VAR"]: e["RUNTIME_KEY"]}))')"

# Create the secret on first use, or merge in values it does not hold yet.
SECRET_FILE="$(mktemp)"
SECRET_STATE="$(SECRET_JSON="$SECRET_JSON" VALUES_JSON="$VALUES_JSON" "$PY" -c '
import json, os, sys
current = json.loads(os.environ["SECRET_JSON"] or "{}")
merged = {**current, **json.loads(os.environ["VALUES_JSON"])}
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(merged))
print("new" if not current else ("same" if merged == current else "changed"))' "$SECRET_FILE")"
SECRET_URI="file://$(native_path "$SECRET_FILE")"
if [ "$SECRET_STATE" = new ]; then
  aws --region "$SECRET_REGION" secretsmanager create-secret --name "$SECRET_ID" \
    --description "pgfoundation Lambda caller keys and Neon DSNs" --secret-string "$SECRET_URI" >/dev/null
  log "created secret $SECRET_ID"
elif [ "$SECRET_STATE" = changed ]; then
  aws --region "$SECRET_REGION" secretsmanager put-secret-value --secret-id "$SECRET_ID" \
    --secret-string "$SECRET_URI" >/dev/null
  log "updated secret $SECRET_ID with new values"
fi
rm -f "$SECRET_FILE"

ACCOUNT="$(aws_ sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
TAG="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo manual)-$(date +%Y%m%d%H%M%S)"
IMAGE="$REGISTRY/$REPO:$TAG"
log "account $ACCOUNT, region $REGION, profile $AWS_PROFILE"

# --- ECR ---------------------------------------------------------------------
log "ECR repository $REPO"
if ! aws_ ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1; then
  aws_ ecr create-repository --repository-name "$REPO" \
    --image-scanning-configuration scanOnPush=true >/dev/null
fi
# Keep the five newest images so storage stays at cents per month.
aws_ ecr put-lifecycle-policy --repository-name "$REPO" --lifecycle-policy-text \
  '{"rules":[{"rulePriority":1,"description":"keep the 5 newest images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' \
  >/dev/null

log "build and push $IMAGE"
aws_ ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
# --provenance/--sbom off: Lambda rejects image indexes that carry attestations.
docker buildx build --platform "linux/$ARCH" --provenance=false --sbom=false \
  -f "$ROOT/deploy/aws-lambda/Dockerfile" -t "$IMAGE" --push "$ROOT/postgresqlmodule/code"

# --- IAM role ----------------------------------------------------------------
log "IAM role $ROLE"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    >/dev/null
  # Logs only: the function needs no other AWS access.
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
fi
ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)"

# --- function environment (secrets travel in a temp file, never on a command line)
ENV_FILE="$(mktemp)"
ERR_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE" "$ERR_FILE"' EXIT
VALUES_JSON="$VALUES_JSON" "$PY" -c \
  'import json, os; print(json.dumps({"Variables": json.loads(os.environ["VALUES_JSON"])}))' \
  > "$ENV_FILE"
ENV_URI="file://$(native_path "$ENV_FILE")"

# --- log group with a retention limit (logs otherwise grow forever) ----------
aws_raw logs create-log-group --log-group-name "/aws/lambda/$FUNCTION" >/dev/null 2>&1 || true
aws_raw logs put-retention-policy --log-group-name "/aws/lambda/$FUNCTION" \
  --retention-in-days "$LOG_RETENTION_DAYS"

# --- Lambda function ---------------------------------------------------------
log "Lambda function $FUNCTION"
if ! aws_ lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
  # A new role takes a few seconds before Lambda can assume it.
  for attempt in 1 2 3 4 5 6 7 8; do
    if aws_ lambda create-function --function-name "$FUNCTION" --package-type Image \
        --code ImageUri="$IMAGE" --role "$ROLE_ARN" --architectures "$ARCH" \
        --memory-size "$MEMORY_MB" --timeout "$TIMEOUT_S" --environment "$ENV_URI" \
        --description "pgfoundation SQL gateway (Neon)" >/dev/null 2>"$ERR_FILE"; then
      break
    fi
    if grep -q "cannot be assumed" "$ERR_FILE" && [ "$attempt" -lt 8 ]; then
      sleep 5
      continue
    fi
    cat "$ERR_FILE" >&2
    exit 1
  done
  aws_ lambda wait function-active-v2 --function-name "$FUNCTION"
else
  aws_ lambda update-function-code --function-name "$FUNCTION" --image-uri "$IMAGE" \
    --architectures "$ARCH" >/dev/null
  aws_ lambda wait function-updated-v2 --function-name "$FUNCTION"
  aws_ lambda update-function-configuration --function-name "$FUNCTION" \
    --memory-size "$MEMORY_MB" --timeout "$TIMEOUT_S" --environment "$ENV_URI" >/dev/null
  aws_ lambda wait function-updated-v2 --function-name "$FUNCTION"
fi

# --- Function URL ------------------------------------------------------------
log "Function URL"
if ! URL="$(aws_ lambda get-function-url-config --function-name "$FUNCTION" \
      --query FunctionUrl --output text 2>/dev/null)"; then
  URL="$(aws_ lambda create-function-url-config --function-name "$FUNCTION" \
    --auth-type NONE --invoke-mode RESPONSE_STREAM --query FunctionUrl --output text)"
fi
# Public at the URL layer; the gateway's bearer-token auth (auth.enabled in
# config/pgfoundation.lambda.yaml) decides who may read or write.
aws_ lambda add-permission --function-name "$FUNCTION" --statement-id FunctionUrlPublicAccess \
  --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE \
  >/dev/null 2>&1 || true
aws_ lambda add-permission --function-name "$FUNCTION" --statement-id FunctionUrlInvokeFunction \
  --action lambda:InvokeFunction --principal '*' --invoked-via-function-url \
  >/dev/null 2>&1 || true

# --- smoke test --------------------------------------------------------------
BASE="${URL%/}"
log "smoke test $BASE"
curl -sS --retry 5 --retry-all-errors --retry-delay 2 -m 30 -w '  [HTTP %{http_code}]\n' "$BASE/health"
curl -sS --retry 3 --retry-all-errors --retry-delay 2 -m 30 -w '  [HTTP %{http_code}]\n' "$BASE/ready"

cat <<EOF

Endpoint:    $BASE
Datasources: neondb (production branch), neondb_dev (branch dev),
             agentruntime, agentobservability and agentbuilder (Agent Runtime database)
Caller keys: secret $SECRET_ID ($API_KEY_VAR, $RUNTIME_KEY_VAR), copies in $ROOT/.env.lambda
Call with:   Authorization: Bearer <key>
EOF
