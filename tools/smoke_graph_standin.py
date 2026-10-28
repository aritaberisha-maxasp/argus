"""Smoke test of the Graph client against the fake Graph running in Docker (ADR 0003).

CI starts the container and runs this from the repository root:
``python tools/smoke_graph_standin.py http://localhost:8081/v1.0 <token>``
"""

import hashlib
import sys
from pathlib import Path

from pydantic import SecretStr

from argus.intake.graph import TOKEN_SCOPE, GraphCallRefused, GraphClient

MAILBOX = "accounts@maxasp.example"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "emails"


class Tokens:
    def __init__(self, token: str) -> None:
        self._token = token

    def token(self, scope: str) -> SecretStr:
        assert scope == TOKEN_SCOPE  # noqa: S101
        return SecretStr(self._token)


def check(ok: bool, what: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        raise SystemExit(1)


def main(base_url: str, token: str) -> None:
    expected = {hashlib.sha256(p.read_bytes()).hexdigest() for p in FIXTURES.glob("*.eml")}
    with GraphClient(Tokens(token), MAILBOX, base_url, standin=True) as graph:
        messages = list(graph.list_inbox())
        check(len(messages) == len(expected), f"Inbox lists all {len(expected)} fixture emails")
        check(len({m.id for m in messages}) == len(messages), "every email has its own ID")
        got = {hashlib.sha256(graph.get_raw(m.id)).hexdigest() for m in messages}
        check(got == expected, "every email reads back byte for byte")
        try:
            graph._http.get("users/someone.else@maxasp.example/mailFolders/inbox/messages")
            refused = False
        except GraphCallRefused:
            refused = True
        check(refused, "another mailbox is refused before it leaves ARGUS")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
