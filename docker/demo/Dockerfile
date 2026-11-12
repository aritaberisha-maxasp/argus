# The stand ins for the ARGUS demo in Azure (#124, ADR 0009): the fake Microsoft Graph, the
# fake DATEV and the Wise stand in, plus `python -m argus.demo` and the synthetic fixtures it
# needs. One image for all of them, so the pipeline builds and pushes one more image only.
# Each container app picks its stand in with its own command (infra/modules/standins.bicep).
# Never for production: everything in here is synthetic test data.
#
# Build from the repository root:
#   docker build -f docker/demo/Dockerfile -t argus-demo .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10002 demo

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src

COPY standins/__init__.py standins/__init__.py
COPY standins/graph/__init__.py standins/graph/app.py standins/graph/
COPY standins/wise/__init__.py standins/wise/app.py standins/wise/
COPY tests/fixtures/emails/*.eml tests/fixtures/emails/
COPY tests/fixtures/master-data /data/master-data
COPY tests/fixtures/datev /data/datev

# The fake DATEV reads its vendors, customers and past postings from /data. The fake Graph and
# `python -m argus.demo seed` find the emails under /app/tests/fixtures/emails.
ENV FAKE_DATEV_DATA=/data \
    FAKE_DATEV_CLIENT=muster-gmbh \
    ARGUS_DEMO_EMAILS=/app/tests/fixtures/emails

USER demo
CMD ["python", "-m", "argus.demo", "--help"]
