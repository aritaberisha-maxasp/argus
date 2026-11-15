"""Send Benjamin one email a day with every open exception (EX-03, #61).

The digest lists each open exception with its reason in plain words, its age in working days,
the day it was opened and a link to it on the queue page. It holds nothing else: no detail
text, no sender, no subject and nothing from an email or a document, because the detail can
hold personal data and an email can be forwarded anywhere (NF-03). The link opens the queue
page, which asks for sign in and shows the rest to the roles allowed to see it (NF-03).

No open exceptions, no digest: nothing is sent and nothing is recorded.

Once a day, even when the job starts twice or crashes halfway. The steps follow the reminder
forward (#27):

1. Only one digest run works at a time: it holds a database lock while it runs. A run that
   finds the lock taken does nothing and counts as a clean run.
2. If today's digest (Berlin date) is already finished in the audit log, nothing happens.
3. If no draft is recorded for today, Graph saves the digest as a draft. Nothing is sent yet.
   A short unit of work records ``exception_digest_drafted`` with the date and the draft's ID.
   A draft whose ID was not recorded (a crash between saving and recording) is never sent; it
   stays in the mailbox's Drafts folder.
4. The recorded draft is sent. Before sending, ARGUS asks Graph whether it is still a draft, so
   a crash after sending does not send again. Graph itself sends a draft once. Right before
   sending, the Graph client reads the draft's recipients again and refuses unless it goes to
   the digest address only, because a person could change the draft meanwhile.
5. A short unit of work records ``exception_digest_sent`` with the outcome. From then on the
   day is finished.

If the recorded draft is gone or was changed, nothing is sent, the day is recorded as finished
with that outcome, and the run counts as failed, so the job alert tells Arita. The next day
starts fresh.

Graph is never called inside a unit of work (PR #79 review). The logs hold the date, counts
and IDs only (NF-03).
"""

import html
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import record_event, unit_of_work
from argus.intake.graph import GraphApiError, GraphCallRefused
from argus.queue_page.views import QueueRow, queue_rows
from argus.working_days import BERLIN, berlin_date

log = logging.getLogger("argus.intake.digest")

DIGEST_ACTOR: Final = Actor.system("exception-digest")
APP_URL_ENV: Final = "ARGUS_APP_URL"
DRAFTED: Final = "exception_digest_drafted"
SENT: Final = "exception_digest_sent"
DIGEST_LOCK: Final = "argus_exception_digest"
# More than this many lines make the email hard to read; the rest are on the queue page.
MAX_LINES: Final = 100

# Outcomes recorded in exception_digest_sent.
SENT_NOW: Final = "sent"
FOUND_SENT: Final = "found_sent"  # Graph said it was no longer a draft: sent before a crash
DRAFT_GONE: Final = "draft_gone"
DRAFT_CHANGED: Final = "draft_changed"
FAILED_OUTCOMES: Final = frozenset({DRAFT_GONE, DRAFT_CHANGED})


class DigestSettingsError(ValueError):
    """A setting the digest needs is missing or malformed. Names the setting, never its value."""


class DigestMailbox(Protocol):
    """The Graph calls the digest needs. :class:`argus.intake.graph.GraphClient` built with a
    digest address is the real one; tests wrap it to simulate crashes."""

    @property
    def digest_to(self) -> str | None: ...

    def create_digest(self, subject: str, html: str) -> str: ...

    def is_draft(self, message_id: str) -> bool: ...

    def send_draft(self, message_id: str, to: str | None = None) -> None: ...


@dataclass(frozen=True)
class DigestResult:
    """What one run did. ``outcome`` is one of the outcomes above, ``nothing_open`` when no
    exception was open, ``already_finished`` when today's digest was finished before this run,
    or ``busy`` when another run held the lock."""

    day: date
    outcome: str
    exception_count: int = 0
    draft_message_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome not in FAILED_OUTCOMES


def app_url_from_environment(environ: Mapping[str, str]) -> str:
    """The address of the ARGUS web app, for the links, from ``ARGUS_APP_URL``: https, a host,
    no query. Set by infra/modules/apps.bicep from the app's own address."""
    value = environ.get(APP_URL_ENV, "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise DigestSettingsError(f"{APP_URL_ENV} must be the https address of the ARGUS app")
    return value


def berlin_today() -> date:
    return berlin_date(datetime.now(BERLIN))


def _age(days: int) -> str:
    if days == 0:
        return "less than 1 working day"
    return f"{days} working day{'' if days == 1 else 's'}"


def render_digest(rows: Sequence[QueueRow], app_url: str, day: date) -> tuple[str, str]:
    """The subject and the HTML body of the digest. Only fixed words, numbers, dates and links:
    nothing from an email, a document or the detail text (NF-03)."""
    count = len(rows)
    noun = "exception" if count == 1 else "exceptions"
    subject = f"ARGUS: {count} open {noun} on {day:%d.%m.%Y}"
    queue = html.escape(f"{app_url}/queue", quote=True)
    lines = []
    for row in rows[:MAX_LINES]:
        link = html.escape(f"{app_url}/queue?exception={row.exception_id}", quote=True)
        opened = berlin_date(row.opened_at)
        lines.append(
            "<tr>"
            f"<td>{html.escape(row.reason_text)}</td>"
            f"<td>{html.escape(_age(row.age_working_days))}</td>"
            f"<td>{opened:%d.%m.%Y}</td>"
            f'<td><a href="{link}">Open exception {row.exception_id}</a></td>'
            "</tr>"
        )
    more = ""
    if count > MAX_LINES:
        rest = count - MAX_LINES
        more = (
            f"<p>{rest} more open {'exception is' if rest == 1 else 'exceptions are'} on the "
            f'<a href="{queue}">exception queue</a>.</p>'
        )
    body = (
        "<html><body>"
        f"<p>Good morning. {count} open {noun} in ARGUS need a person, oldest first.</p>"
        "<table>"
        "<tr><th>Reason</th><th>Age</th><th>Opened</th><th>Link</th></tr>"
        + "".join(lines)
        + "</table>"
        + more
        + f'<p>All open exceptions: <a href="{queue}">exception queue</a>. '
        "The links ask you to sign in.</p>"
        "<p>This email is sent by ARGUS once a day while exceptions are open.</p>"
        "</body></html>"
    )
    return subject, body


@dataclass(frozen=True)
class _Recorded:
    draft_message_id: str | None
    finished_outcome: str | None
    exception_count: int = 0


def _recorded(connection: Connection, day: date) -> _Recorded:
    """What the audit log says about the digest of ``day``: the first recorded draft and the
    outcome, if it is finished."""
    rows = connection.execute(
        text(
            """
            select event_type, details from audit_event
            where event_type in (:drafted, :sent) and details ->> 'day' = :day
            order by chain_seq
            """
        ),
        {"drafted": DRAFTED, "sent": SENT, "day": day.isoformat()},
    ).all()
    drafted: str | None = None
    finished: str | None = None
    count = 0
    details: Any
    for event_type, details in rows:
        if event_type == DRAFTED and drafted is None:
            drafted = str(details["draft_message_id"])
            count = int(details.get("exception_count", 0))
        if event_type == SENT and finished is None:
            finished = str(details["outcome"])
    return _Recorded(drafted, finished, count)


def _lock_day(connection: Connection) -> None:
    """Writes about the digest wait for each other, so two cannot both record a draft."""
    connection.execute(text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": DIGEST_LOCK})


@contextmanager
def digest_lock(engine: Engine) -> Iterator[bool]:
    """Try to take the digest lock for the whole run. Yields False when another run holds it.

    A session lock on its own connection, outside any transaction, as the sweep does, so it is
    held while Graph is called without keeping a transaction open."""
    with engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT")
        got = bool(
            connection.execute(
                text("select pg_try_advisory_lock(hashtext(:key || '_run'))"),
                {"key": DIGEST_LOCK},
            ).scalar()
        )
        try:
            yield got
        finally:
            if got:
                connection.execute(
                    text("select pg_advisory_unlock(hashtext(:key || '_run'))"),
                    {"key": DIGEST_LOCK},
                )


def _record_draft(engine: Engine, day: date, draft_id: str, rows: Sequence[QueueRow]) -> str:
    """Step 3: record the draft, unless another run recorded one for this day first. Returns
    the recorded draft's ID, which is the only one ever sent."""
    with unit_of_work(engine, DIGEST_ACTOR) as db:
        _lock_day(db)
        existing = _recorded(db, day).draft_message_id
        if existing is not None:
            log.warning("Digest for %s already recorded, draft left unsent", day)
            return existing
        details: dict[str, Any] = {
            "day": day.isoformat(),
            "draft_message_id": draft_id,
            "exception_count": len(rows),
            "exception_ids": [row.exception_id for row in rows],
        }
        record_event(db, DRAFTED, details=details)
    return draft_id


def _send(graph: DigestMailbox, draft_id: str) -> str:
    """Step 4. Returns the outcome."""
    try:
        if not graph.is_draft(draft_id):
            log.info("Digest draft was found already sent")
            return FOUND_SENT
        graph.send_draft(draft_id, to=graph.digest_to)
    except GraphApiError as error:
        if error.status != 404:
            raise  # Graph unreachable or throttled: the next run tries again
        log.warning("Digest draft is gone, nothing sent")
        return DRAFT_GONE
    except GraphCallRefused:
        log.warning("Digest draft was changed, nothing sent")
        return DRAFT_CHANGED
    return SENT_NOW


def _finish(engine: Engine, day: date, draft_id: str, outcome: str) -> str:
    """Step 5: record the outcome, unless the day was finished meanwhile. Returns the outcome
    the audit log holds for the day."""
    with unit_of_work(engine, DIGEST_ACTOR) as db:
        _lock_day(db)
        finished = _recorded(db, day).finished_outcome
        if finished is not None:
            return finished
        record_event(
            db,
            SENT,
            details={"day": day.isoformat(), "draft_message_id": draft_id, "outcome": outcome},
        )
    return outcome


def send_daily_digest(
    engine: Engine, graph: DigestMailbox, app_url: str, today: date | None = None
) -> DigestResult:
    """Send today's digest of open exceptions to the digest address, once.

    Safe to call again after a crash at any point and safe to start twice: it carries on from
    the last recorded step and never sends a second digest for the same Berlin date.
    ``today`` is for tests."""
    if not graph.digest_to:
        raise DigestSettingsError("the Graph client has no digest address, set ARGUS_DIGEST_TO")
    day = today or berlin_today()
    with digest_lock(engine) as got:
        if not got:
            log.info("Another digest run holds the lock, nothing to do")
            return DigestResult(day, "busy")
        with engine.connect() as connection:
            recorded = _recorded(connection, day)
            rows = [] if recorded.draft_message_id else queue_rows(connection)
        if recorded.finished_outcome is not None:
            return DigestResult(
                day, "already_finished", recorded.exception_count, recorded.draft_message_id
            )
        draft_id = recorded.draft_message_id
        count = recorded.exception_count
        if draft_id is None:
            if not rows:
                log.info("No open exceptions on %s, no digest", day)
                return DigestResult(day, "nothing_open")
            subject, body = render_digest(rows, app_url, day)
            draft_id = _record_draft(engine, day, graph.create_digest(subject, body), rows)
            count = len(rows)
        outcome = _finish(engine, day, draft_id, _send(graph, draft_id))
        log.info("Digest for %s finished: %s, %d exception(s)", day, outcome, count)
        return DigestResult(day, outcome, count, draft_id)
