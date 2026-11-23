"""Each job builds its Graph client with only the calls it needs (#168, NF-03, NF-01).

A call outside a job's list is refused inside the client, before anything goes to Graph. The
tests below try every kind of call with every job's client against a transport that records
what reaches it. The integration tests in tests/integration/test_job_graph_calls.py show that
each job still does its whole work with its own list.
"""

from collections.abc import Callable, Iterator

import httpx
import pytest

from argus.intake import jobs
from argus.intake.graph import ALL_CALLS, ApprovalMail, GraphCall, GraphCallRefused, GraphClient
from argus.intake.jobs import (
    APPROVAL_REQUEST_CALLS,
    DIGEST_CALLS,
    REMINDER_FORWARD_CALLS,
    REMOVAL_CALLS,
    STATEMENT_IMPORT_CALLS,
    STATEMENT_REMINDER_CALLS,
    SWEEP_CALLS,
    SWEEP_JOB_CALLS,
    digest_services_from_env,
    removal_services_from_env,
    services_from_env,
    statement_services_from_env,
)
from argus.mailbox_page.web import MAILBOX_PAGE_CALLS
from standins.graph.app import MAILBOX
from tests.unit.intake.graph_helpers import StaticTokens
from tests.unit.intake.test_jobs import DEMO

BASE_URL = "http://graph.example/v1.0"
FORWARD_TO = "reminders@maxasp.example"
DIGEST_TO = "digest@maxasp.example"
EMAIL = "AAMkAGEmail0001"
FOLDER = "AAMkAGFolder0001"
USER = f"users/{MAILBOX}"

JOBS: dict[str, frozenset[GraphCall]] = {
    "sweep": SWEEP_JOB_CALLS,
    "removal": REMOVAL_CALLS,
    "digest": DIGEST_CALLS,
    "statement reminder": STATEMENT_REMINDER_CALLS,
    "mailbox page": MAILBOX_PAGE_CALLS,
    "approval requests": APPROVAL_REQUEST_CALLS,
}

MAIL = ApprovalMail(
    request_id=1,
    case_id=7,
    recipient_address="approver@maxasp.example",
    recipient_name="Alice Test",
    on_behalf_of_name=None,
    vendor_name="Muster GmbH",
    document_number="RE-1",
    gross_cents=11900,
    currency="EUR",
    credit_note=False,
    link="https://argus.example/approvals/7",
)


def list_statement_folder(graph: GraphClient) -> object:
    # The folder was found already; this is the listing itself.
    graph._transport.listable_folder = FOLDER
    return graph._get(f"{USER}/mailFolders/{FOLDER}/messages")


# One way to make each kind of call. Each one's first request is that call.
CALLS: dict[GraphCall, Callable[[GraphClient], object]] = {
    GraphCall.LIST_INBOX: lambda g: next(g.list_inbox(), None),
    GraphCall.READ_EMAIL: lambda g: g.get_raw(EMAIL),
    GraphCall.READ_MESSAGE: lambda g: g.is_draft(EMAIL),
    GraphCall.FIND_FOLDERS: lambda g: g.find_folder(None, "ARGUS"),
    GraphCall.CREATE_FOLDER: lambda g: g.create_folder(None, "ARGUS"),
    GraphCall.READ_INBOX_ID: lambda g: g.inbox(),
    GraphCall.READ_DELETED_ITEMS_ID: lambda g: g.deleted_items(),
    GraphCall.MOVE: lambda g: g.move(EMAIL, FOLDER),
    GraphCall.DELETE: lambda g: g._send("DELETE", f"{USER}/messages/{EMAIL}"),
    GraphCall.LIST_STATEMENT_FOLDER: list_statement_folder,
    GraphCall.LIST_ANY_FOLDER: lambda g: g.folder_messages(FOLDER),
    GraphCall.FORWARD: lambda g: g.create_forward(EMAIL, "Zur Info"),
    GraphCall.SEND_NOTICE: lambda g: g.send_notice("ARGUS: Kontoauszug fehlt", "Bitte senden."),
    GraphCall.CREATE_DIGEST: lambda g: g.create_digest("ARGUS: offene Fälle", "<p>2</p>"),
    GraphCall.SEND_DRAFT: lambda g: g._post(f"{USER}/messages/{EMAIL}/send"),
    GraphCall.SEND_APPROVAL_REQUEST: lambda g: g.send_approval_request(MAIL),
}


class Recorder:
    """Stands in for Graph: records each request that reaches it and answers 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"id": EMAIL, "value": []})


def client(calls: frozenset[GraphCall], recorder: Recorder) -> GraphClient:
    """A client with every address set and the stand in switch on, so that only ``calls``
    decides what may go out. The approval client cannot hold the other addresses at all."""
    approvals = GraphCall.SEND_APPROVAL_REQUEST in calls
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=httpx.MockTransport(recorder),
        standin=True,
        sleep=lambda _: None,
        forward_to=None if approvals else FORWARD_TO,
        digest_to=None if approvals else DIGEST_TO,
        statement_folder="ARGUS/Kontoauszüge",
        calls=calls,
    )


# Listing any folder in the demo takes in the Inbox and the statement folder too.
INCLUDED = {GraphCall.LIST_ANY_FOLDER: {GraphCall.LIST_INBOX, GraphCall.LIST_STATEMENT_FOLDER}}


def allowed(calls: frozenset[GraphCall]) -> set[GraphCall]:
    return set(calls).union(*(INCLUDED.get(call, set()) for call in calls))


def every_job_and_call() -> Iterator[tuple[str, GraphCall]]:
    for job in JOBS:
        for call in GraphCall:
            yield job, call


def test_every_kind_of_call_is_tried() -> None:
    assert set(CALLS) == set(GraphCall)
    # Only a client built for it alone may send an approval request (#191).
    assert set(GraphCall) - {GraphCall.SEND_APPROVAL_REQUEST} == ALL_CALLS


@pytest.mark.req("NF-03", "NF-01")
@pytest.mark.parametrize(("job", "call"), list(every_job_and_call()))
def test_a_call_outside_the_jobs_list_is_refused_before_it_goes_to_graph(
    job: str, call: GraphCall
) -> None:
    recorder = Recorder()
    graph = client(JOBS[job], recorder)

    if call in allowed(JOBS[job]):
        try:
            CALLS[call](graph)
        except GraphCallRefused:
            pytest.fail(f"the {job} needs to {call} and was refused")
        except Exception:  # noqa: S110  the made up answer may not fit; what went out counts
            pass
        assert recorder.requests, f"the {job} could not {call}"
    else:
        with pytest.raises(GraphCallRefused):
            CALLS[call](graph)
        assert recorder.requests == []


@pytest.mark.req("NF-03", "EX-03")
def test_the_digest_may_only_create_read_back_and_send_its_message() -> None:
    assert {GraphCall.CREATE_DIGEST, GraphCall.READ_MESSAGE, GraphCall.SEND_DRAFT} == DIGEST_CALLS


@pytest.mark.req("NF-03", "NF-01")
def test_only_the_removal_may_delete_and_it_may_not_move_list_or_send() -> None:
    deleting = [job for job, calls in JOBS.items() if GraphCall.DELETE in calls]
    assert deleting == ["removal"]
    assert REMOVAL_CALLS.isdisjoint(
        {
            GraphCall.MOVE,
            GraphCall.CREATE_FOLDER,
            GraphCall.LIST_INBOX,
            GraphCall.FORWARD,
            GraphCall.SEND_NOTICE,
            GraphCall.CREATE_DIGEST,
            GraphCall.SEND_DRAFT,
        }
    )


@pytest.mark.req("NF-03")
def test_the_sweep_job_is_the_sweep_the_reminder_forward_and_the_statement_import() -> None:
    assert SWEEP_JOB_CALLS == SWEEP_CALLS | REMINDER_FORWARD_CALLS | STATEMENT_IMPORT_CALLS
    assert GraphCall.CREATE_DIGEST not in SWEEP_JOB_CALLS
    assert GraphCall.LIST_ANY_FOLDER not in SWEEP_JOB_CALLS


# Each job builds its client with its own list.


@pytest.mark.req("NF-03")
def test_the_sweep_job_builds_its_client_with_its_own_list() -> None:
    services = services_from_env(DEMO)
    try:
        assert services.graph.calls == SWEEP_JOB_CALLS
    finally:
        services.close()


@pytest.mark.req("NF-03")
def test_the_removal_job_builds_its_client_with_its_own_list() -> None:
    assert jobs.BUILDERS["removal"] is removal_services_from_env
    services = removal_services_from_env(DEMO)
    try:
        assert services.graph.calls == REMOVAL_CALLS
    finally:
        services.close()


@pytest.mark.req("NF-03")
def test_the_statement_job_builds_its_client_with_its_own_list() -> None:
    services = statement_services_from_env(DEMO)
    try:
        assert services.graph.calls == STATEMENT_REMINDER_CALLS
    finally:
        services.close()


@pytest.mark.req("NF-03")
def test_the_digest_job_builds_its_client_with_its_own_list() -> None:
    environ = {**DEMO, "ARGUS_DIGEST_TO": DIGEST_TO, "ARGUS_APP_URL": "https://argus.example"}
    services = digest_services_from_env(environ)
    try:
        assert services.graph.calls == DIGEST_CALLS
    finally:
        services.close()


@pytest.mark.req("NF-03", "AP-02")
def test_only_the_approval_client_may_send_an_approval_request_and_nothing_else() -> None:
    sending = [job for job, calls in JOBS.items() if GraphCall.SEND_APPROVAL_REQUEST in calls]
    assert sending == ["approval requests"]
    assert {GraphCall.SEND_APPROVAL_REQUEST} == APPROVAL_REQUEST_CALLS
    with pytest.raises(ValueError, match="approval requests only"):
        GraphClient(
            StaticTokens(),
            MAILBOX,
            BASE_URL,
            standin=True,
            calls=APPROVAL_REQUEST_CALLS | {GraphCall.SEND_NOTICE},
        )
    with pytest.raises(ValueError, match="approval requests only"):
        GraphClient(
            StaticTokens(),
            MAILBOX,
            BASE_URL,
            standin=True,
            forward_to=FORWARD_TO,
            calls=APPROVAL_REQUEST_CALLS,
        )


@pytest.mark.req("NF-03", "AP-02")
def test_the_sweep_job_builds_the_approval_client_with_its_own_list() -> None:
    services = services_from_env({**DEMO, "ARGUS_APP_URL": "https://argus.example"})
    try:
        approvals = services.sorting.approvals
        assert approvals is not None
        assert approvals.mailbox.calls == APPROVAL_REQUEST_CALLS  # type: ignore[attr-defined]
        assert approvals.link(12) == "https://argus.example/approvals/12"
        assert GraphCall.SEND_APPROVAL_REQUEST not in services.graph.calls
    finally:
        services.close()


@pytest.mark.req("AP-02")
def test_without_the_app_address_checked_invoices_wait() -> None:
    services = services_from_env(DEMO)
    try:
        assert services.sorting.approvals is None
    finally:
        services.close()
