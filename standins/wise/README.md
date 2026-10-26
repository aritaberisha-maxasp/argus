# Wise stand in

A fake Wise API for development and CI, because the build environment cannot reach Wise (ADR 0004). It answers the calls on the ARGUS Wise allowlist (`src/argus/wise/allowlist.py`) and records every recipient, quote, transfer and batch group it receives. It has no funding or release endpoint, just like Wise for a German personal token, so a funding call gets a 404.

All data is synthetic and lives in memory until the container stops.

## Run it

From the repository root:

```bash
docker build -f standins/wise/Dockerfile -t argus-wise-standin .
docker run --rm -p 8080:8080 -e WISE_STANDIN_TOKEN=local-test-token argus-wise-standin
```

Point ARGUS at it with base URL `http://localhost:8080` and the same token. Without `WISE_STANDIN_TOKEN` it accepts any bearer token.

## Look at what ARGUS handed over

- `GET /_standin/records` lists recipients, quotes, batch groups and transfers.
- `POST /_standin/reset` clears them.

Transfers stay at `incoming_payment_waiting`, because nobody can fund them here.
