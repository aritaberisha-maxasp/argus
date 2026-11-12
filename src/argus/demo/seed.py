"""Put the demo emails into the fake mailbox (#124)."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email import message_from_bytes
from pathlib import Path

import httpx

from argus.connections import GRAPH_URL_ENV, Connections, is_standin_address
from argus.demo.common import DemoRefused, control_root
from argus.demo.data import DEMO_EMAILS, DemoEmail

# How long seed waits for the fake mailbox to answer, for example right after the Demo
# workflow restarted it or Azure pulled the image anew (review of PR 130).
READY_TIMEOUT = 180.0
READY_INTERVAL = 5.0


@dataclass(frozen=True)
class SeededEmail:
    file: str
    shows: str
    graph_id: str


def seed(
    connections: Connections,
    folder: Path,
    http: httpx.Client | None = None,
    emails: Sequence[DemoEmail] = DEMO_EMAILS,
    *,
    timeout: float = READY_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SeededEmail]:
    """Add each demo email to the Inbox of the fake mailbox. Seeding twice adds nothing new:
    the fake Graph keys an email by its content, as Graph keeps one copy of one email."""
    if not connections.demo:
        raise DemoRefused("demo mode is off, so there is no fake mailbox to seed")
    if not connections.graph_url or not is_standin_address(connections.graph_url):
        raise DemoRefused(f"{GRAPH_URL_ENV} must point at the fake Graph")
    missing = [email.file for email in emails if not (folder / email.file).is_file()]
    if missing:
        raise DemoRefused(f"demo emails not found in {folder}: {', '.join(missing)}")
    for email in emails:
        if not _synthetic(folder / email.file):
            raise DemoRefused(f"{email.file} is not addressed to the synthetic test mailbox")
    client = http or httpx.Client(timeout=30)
    seeded = []
    try:
        root = control_root(connections.graph_url)
        wait_until_ready(client, root, timeout, clock, sleep)
        for email in emails:
            answer = client.post(
                f"{root}/_standin/messages",
                content=(folder / email.file).read_bytes(),
                headers={"Content-Type": "message/rfc822"},
            )
            answer.raise_for_status()
            seeded.append(SeededEmail(email.file, email.shows, str(answer.json()["id"])))
    finally:
        if http is None:
            client.close()
    return seeded


def wait_until_ready(
    client: httpx.Client,
    root: str,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Wait until the fake mailbox answers. Not reachable yet and 5xx count as starting up;
    anything else, such as 404 from something that is not the fake Graph, is a real error."""
    deadline = clock() + timeout
    while True:
        try:
            answer = client.get(f"{root}/_standin/records")
        except httpx.TransportError:
            answer = None
        if answer is not None and answer.status_code < 500:
            answer.raise_for_status()
            return
        if clock() + READY_INTERVAL > deadline:
            raise DemoRefused(f"the fake mailbox did not answer within {timeout:.0f} seconds")
        sleep(READY_INTERVAL)


def _synthetic(path: Path) -> bool:
    """Fixture emails only use the reserved domains .example and .invalid (RFC 2606)."""
    parsed = message_from_bytes(path.read_bytes())
    for header in ("From", "To"):
        for address in str(parsed.get(header, "")).replace(">", " ").split():
            if "@" in address and not address.rstrip(",").endswith((".example", ".invalid")):
                return False
    return True
