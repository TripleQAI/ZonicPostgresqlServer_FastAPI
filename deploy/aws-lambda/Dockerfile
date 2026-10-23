# pgfoundation on AWS Lambda — the service image plus the AWS Lambda Web Adapter.
#
# The adapter runs as a Lambda extension, starts the unchanged uvicorn app, waits
# for /health, and translates Function URL events to plain HTTP. No handler code,
# no Mangum, no change to pgfoundation_api.
#
# Build context is postgresqlmodule/code (same as the service Dockerfile):
#   docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
#     -f deploy/aws-lambda/Dockerfile postgresqlmodule/code
#
# --provenance=false matters: Lambda rejects image indexes that carry
# attestation manifests.

# --- build stage -----------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
# Runtime dependencies only: everything above the "dev / test" marker. A smaller
# image means a faster Lambda cold start.
RUN sed '/^# --- dev \/ test ---/,$d' requirements.txt > requirements.runtime.txt \
 && pip install --no-cache-dir --prefix=/install -r requirements.runtime.txt

# --- runtime stage -----------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.12-slim

# AWS Lambda Web Adapter (pinned).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local
WORKDIR /app
COPY pgfoundation/     ./pgfoundation/
COPY pgfoundation_api/ ./pgfoundation_api/
COPY config/           ./config/

ENV PGF_CONFIG=/app/config/pgfoundation.lambda.yaml \
    PYTHONUNBUFFERED=1 \
    AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_INVOKE_MODE=response_stream

RUN useradd --system --uid 10001 pgf
USER pgf

EXPOSE 8000

# One worker per execution environment: Lambda scales by environments, so the
# connection-budget arithmetic in pgfoundation.lambda.yaml stays a single variable.
CMD ["uvicorn", "pgfoundation_api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
