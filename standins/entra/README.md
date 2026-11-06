# Fake Microsoft Entra ID

A fake sign in for development and CI, so ARGUS can be used and tested without a Microsoft account (#23, [ADR 0008](../../docs/adr/0008-signing-in-to-argus.md)).

It signs access tokens shaped like Entra v2 tokens for the ARGUS API and publishes its public key at the same path Entra uses. ARGUS checks these tokens with the same code it uses in Azure. The only difference is `ARGUS_ENTRA_AUTHORITY`, which points here instead of `https://login.microsoftonline.com`. ARGUS accepts a plain http authority only on `localhost` or `127.0.0.1`, so the fake can never be trusted in Azure.

All IDs are made up. The signing key is new each time the fake starts and stays in memory.

## Run it

From the repository root, in one terminal:

```bash
.venv/bin/uvicorn standins.entra.app:app --port 8092
```

In another, start ARGUS with the settings the fake prints:

```bash
eval "$(curl -s localhost:8092/_standin/env | python -c 'import json,sys; [print(f"export {k}={v}") for k,v in json.load(sys.stdin).items()]')"
.venv/bin/uvicorn argus.app:app --port 8000
```

Get a token for a person with a role and call ARGUS with it:

```bash
TOKEN=$(curl -s -X POST localhost:8092/_standin/token -H 'content-type: application/json' \
  -d '{"object_id": "11111111-2222-4333-8444-555555555555", "roles": ["finance"]}' | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s localhost:8000/me -H "Authorization: Bearer $TOKEN"
```

Roles are `approver`, `finance` and `admin`. A token without any role signs in but gets no data.

## In tests

`FakeEntra` in `standins/entra/app.py` gives a verifier that checks its tokens without HTTP, and helpers to build tokens that are expired, from another tenant, for another app, signed with another key and so on. See `tests/unit/test_sign_in.py`.
