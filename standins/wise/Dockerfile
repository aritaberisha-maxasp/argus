# Wise stand in for development and CI (ADR 0004). Build from the repository root:
#   docker build -f standins/wise/Dockerfile -t argus-wise-standin .
FROM python:3.12-slim

RUN pip install --no-cache-dir "fastapi==0.115.12" "uvicorn==0.34.2" \
    && useradd --create-home standin

WORKDIR /app
COPY standins/__init__.py standins/__init__.py
COPY standins/wise/__init__.py standins/wise/app.py standins/wise/

USER standin
EXPOSE 8080
CMD ["uvicorn", "standins.wise.app:app", "--host", "0.0.0.0", "--port", "8080"]
