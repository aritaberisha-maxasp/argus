# Fake Microsoft Graph

A fake Graph for development and CI, so the mailbox sweep runs without a Microsoft account (ADR 0003). It serves the accounts mailbox from the 50 synthetic fixture emails in `tests/fixtures/emails` and answers the Graph calls ARGUS makes today: list the Inbox (with paging), read one email as its raw `.eml` file, find and create folders, read which folder an email is in, move an email and delete it, and forward a payment reminder through a saved draft (save the forward, check it is still a draft and who it goes to, send it).

It behaves like Graph where it matters for ARGUS:

- IDs are immutable only when the request sends `Prefer: IdType="ImmutableId"`. Without the header you get different IDs, as with Graph after a move.
- Only the one mailbox (`accounts@maxasp.example`) is open. Any other mailbox gets `403 ErrorAccessDenied`, like an app scoped by Exchange.
- It can answer with `429` and `Retry-After` on demand, to test throttling.
- A forward is saved as a draft that carries every attachment of the original. A draft can be sent once; sending it again gets `400`, as with Graph.
- A deleted email goes to Deleted Items and can still be read there, as in Graph. Deleting it again from Deleted Items makes it answer `404`.
- Creating a folder that already exists under the same parent gets `409 ErrorFolderExists`, and only `$filter=displayName eq '<name>'` is understood on folder lists.

All data is synthetic and lives in memory until the container stops.

## Run it

From the repository root:

```bash
docker build -f standins/graph/Dockerfile -t argus-graph-standin .
TOKEN=$(python -c "from standins.graph.app import fake_token; print(fake_token(['Mail.ReadWrite', 'Mail.Send']))")
docker run --rm -p 8081:8081 -e GRAPH_STANDIN_TOKEN="$TOKEN" argus-graph-standin
```

Point the Graph client at `http://localhost:8081/v1.0` with `standin=True`, the only way it accepts plain http, and a token provider that returns the same token. The token is shaped like an Entra token so the client can check its permissions; it is not signed and works nowhere else.

## Control calls

- `POST /_standin/messages` with a raw `.eml` body adds an email to the Inbox.
- `DELETE /_standin/messages/{id}` takes one out, as if ARGUS had filed it.
- `POST /_standin/throttle?status=429&retry_after=1&times=1` makes the next calls fail.
- `PATCH /_standin/drafts/{id}` with `{"to": [...], "cc": [...], "bcc": [...]}` changes a saved draft, as a person in Outlook could.
- `DELETE /_standin/drafts/{id}` deletes a draft, as a person could.
- `POST /_standin/drafts/{id}/send` sends a draft the way a person would, outside ARGUS.
- `GET /_standin/records` shows how many emails are in the Inbox, how often each was read, the folders, how many emails were deleted, how many drafts were saved, and every forward that was sent with its recipients and attachment fingerprints.
- `POST /_standin/reset` goes back to the fixture emails.
