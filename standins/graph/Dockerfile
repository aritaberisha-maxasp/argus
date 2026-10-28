# Fake Microsoft Graph for development and CI (ADR 0003). Build from the repository root:
#   docker build -f standins/graph/Dockerfile -t argus-graph-standin .
FROM python:3.12-slim

RUN pip install --no-cache-dir "fastapi==0.115.12" "uvicorn==0.34.2" \
    && useradd --create-home standin

WORKDIR /app
COPY standins/__init__.py standins/__init__.py
COPY standins/graph/__init__.py standins/graph/app.py standins/graph/
COPY tests/fixtures/emails/*.eml tests/fixtures/emails/

USER standin
EXPOSE 8081
CMD ["uvicorn", "standins.graph.app:app", "--host", "0.0.0.0", "--port", "8081"]
