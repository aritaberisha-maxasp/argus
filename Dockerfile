# The ARGUS web app (ADR 0001, issue #81). The deploy pipeline builds this from the repository
# root and pushes it to ARGUS's own Container Registry:
#   docker build --build-arg ARGUS_BUILD=<commit> -t argus .
# The image holds no secret. In Azure every secret arrives as an environment variable from
# Key Vault, and the database takes an Entra token instead of a password.
FROM python:3.12-slim

# The commit the image is built from. /health shows it, so the smoke test knows the new
# version is the one answering.
ARG ARGUS_BUILD=dev
ENV ARGUS_BUILD=${ARGUS_BUILD} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 argus

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src

USER argus
EXPOSE 8000
# Container Apps ends TLS in front of the app and passes the original scheme on.
CMD ["uvicorn", "argus.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
