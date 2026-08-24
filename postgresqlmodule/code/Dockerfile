# See docs (infra repo) TripleQAI-enterprise-aws-cicd-deployment-plan.md §12.4 for
# the reasoning behind every line here — this file intentionally mirrors that spec.

# --- build stage -----------------------------------------------------------
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim
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
