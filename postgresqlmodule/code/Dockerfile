# See docs (infra repo) TripleQAI-enterprise-aws-cicd-deployment-plan.md §12.4 for
# the reasoning behind every line here — this file intentionally mirrors that spec.

# --- build stage -----------------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim

# Picks up point-fixes already available in Debian's repos (e.g. CVE-2026-14456,
# fixed in libssl3t64/openssl 3.5.7-1~deb13u2) without waiting for a new upstream
# python:3.12-slim tag. Everything else the trivy scan flags on this base image
# has no upstream fix yet — see .trivyignore (repo root) for the documented list.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local
WORKDIR /app
COPY pgfoundation/     ./pgfoundation/
COPY pgfoundation_api/ ./pgfoundation_api/
COPY config/           ./config/

RUN useradd --system --uid 10001 pgf
USER pgf

EXPOSE 8000

# The plain uvicorn CLI is correct here — run_local.py exists only because
# psycopg's async driver needs a SelectorEventLoop on Windows; Linux containers use
# epoll and are unaffected. One worker per replica: scale by replica count, not
# worker count, so the connection-budget arithmetic stays a single variable — see
# the deployment plan §12.3.
CMD ["uvicorn", "pgfoundation_api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
