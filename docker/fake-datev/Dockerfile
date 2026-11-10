# The fake DATEV for development and CI (ADR 0002). Never deploy this image to Azure: the demo
# in Azure runs the fake DATEV from docker/demo/Dockerfile instead (#124, ADR 0009).
# Build from the repository root:
#   docker build -f docker/fake-datev/Dockerfile -t argus-fake-datev .
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . "uvicorn>=0.32,<1"

COPY tests/fixtures/master-data /data/master-data
COPY tests/fixtures/datev /data/datev
ENV FAKE_DATEV_DATA=/data \
    FAKE_DATEV_CLIENT=muster-gmbh

USER nobody
# 58452 is the port DATEVconnect listens on
EXPOSE 58452
HEALTHCHECK --interval=5s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:58452/health')"
CMD ["uvicorn", "argus.datev.fake:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "58452"]
