"""Original documents kept unaltered with their fingerprint (issue #20, ADR 0001).

The first group runs against Azurite and matches the acceptance criteria of #20 for the blob
store. The second group adds PostgreSQL and checks that the fingerprint is on the case and in
the audit log, and that neither can be changed.
"""

import hashlib
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from azure.storage.blob import BlobClient, BlobServiceClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from argus.cases.model import Actor, CaseKind, CaseStatus, ClosedReason
from argus.cases.store import (
    CaseRuleBroken,
    DuplicateRefused,
    NotAllowed,
    create_case,
    move_case,
    record_event,
    trace,
    unit_of_work,
)
from argus.documents.email import EML_CONTENT_TYPE, store_email
from argus.documents.records import (
    case_documents,
    link_document_to_case,
    record_document,
    record_email_documents,
)
from argus.documents.store import (
    AzureDocumentStore,
    DocumentNotFound,
    FingerprintMismatch,
    location_of,
)

EMAILS = Path(__file__).resolve().parents[1] / "fixtures" / "emails"
INTAKE = Actor.system("intake")
_email_ids = iter(range(5_000_001, 6_000_000))


def fixture(name: str) -> bytes:
    return (EMAILS / f"{name}.eml").read_bytes()


def attachment_parts(raw: bytes) -> list[EmailMessage]:
    message = message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)
    return list(message.iter_attachments())


def blob_client(documents: AzureDocumentStore, connection_string: str, location: str) -> BlobClient:
    service = BlobServiceClient.from_connection_string(connection_string)
    return service.get_blob_client(documents.container_name, location)


# --- AC: storing an .eml and reading it back gives identical bytes and the same SHA-256 ---


@pytest.mark.req("NF-01")
def test_an_email_comes_back_byte_for_byte_with_the_same_fingerprint(
    documents: AzureDocumentStore,
) -> None:
    raw = fixture("E01_buerobedarf-rechnung")
    stored = documents.put(raw, EML_CONTENT_TYPE)

    assert stored.sha256 == hashlib.sha256(raw).hexdigest()
    assert stored.location == f"sha256/{stored.sha256}"
    assert stored.size == len(raw)
    back = documents.get(stored.sha256)
    assert back == raw
    assert hashlib.sha256(back).hexdigest() == stored.sha256


@pytest.mark.req("NF-01")
def test_every_fixture_email_and_attachment_comes_back_unaltered(
    documents: AzureDocumentStore,
) -> None:
    for path in sorted(EMAILS.glob("*.eml")):
        raw = path.read_bytes()
        stored = store_email(documents, raw)
        assert documents.get(stored.raw.sha256) == raw, path.name
        expected = [p.get_payload(decode=True) for p in attachment_parts(raw)]
        assert [documents.get(a.document.sha256) for a in stored.attachments] == expected


@pytest.mark.req("NF-01")
def test_the_content_type_is_kept_on_the_blob(
    documents: AzureDocumentStore, blob_connection_string: str
) -> None:
    stored = store_email(documents, fixture("E09_buerobedarf-zugferd"))
    blob = blob_client(documents, blob_connection_string, stored.attachments[0].document.location)
    properties = blob.get_blob_properties()
    assert properties.content_settings.content_type == "application/pdf"
    assert properties.metadata["sha256"] == stored.attachments[0].document.sha256


# --- AC: e-invoice XML attachments are stored as they arrived, never only a converted copy ---


@pytest.mark.req("NF-02", "NF-01")
def test_an_xrechnung_xml_is_stored_exactly_as_it_arrived(documents: AzureDocumentStore) -> None:
    raw = fixture("E08_cloud-hosting-xrechnung")
    stored = store_email(documents, raw)

    [part] = attachment_parts(raw)
    arrived = part.get_payload(decode=True)
    assert isinstance(arrived, bytes)
    [xml] = stored.attachments
    assert xml.filename == "Rechnung_BCH-26-10-0057_xrechnung.xml"
    assert xml.document.content_type == "application/xml"
    assert xml.document.sha256 == hashlib.sha256(arrived).hexdigest()
    assert documents.get(xml.document.sha256) == arrived
    assert arrived.lstrip().startswith(b"<?xml")


@pytest.mark.req("NF-02", "NF-01")
def test_an_xrechnung_with_a_pdf_keeps_both_files(documents: AzureDocumentStore) -> None:
    stored = store_email(documents, fixture("E21_ausgang-xrechnung"))
    assert [a.document.content_type for a in stored.attachments] == [
        "application/xml",
        "application/pdf",
    ]


@pytest.mark.req("NF-02", "NF-01")
def test_a_zugferd_pdf_keeps_its_embedded_xml(documents: AzureDocumentStore) -> None:
    raw = fixture("E09_buerobedarf-zugferd")
    stored = store_email(documents, raw)
    [pdf] = stored.attachments
    back = documents.get(pdf.document.sha256)
    assert back == attachment_parts(raw)[0].get_payload(decode=True)
    # The PDF is stored whole, so the XML embedded in it is still there.
    assert b"factur-x.xml" in back or b"zugferd-invoice.xml" in back


# --- AC: storing the same file twice does not overwrite the first copy ---


@pytest.mark.req("NF-01")
def test_storing_the_same_file_twice_keeps_the_first_copy(
    documents: AzureDocumentStore, blob_connection_string: str
) -> None:
    raw = fixture("E02_cloud-hosting-rechnung")
    first = documents.put(raw, EML_CONTENT_TYPE)
    blob = blob_client(documents, blob_connection_string, first.location)
    before = blob.get_blob_properties()

    again = documents.put(raw, "application/octet-stream")

    after = blob.get_blob_properties()
    assert again.location == first.location
    assert again.content_type == EML_CONTENT_TYPE
    assert (after.etag, after.last_modified) == (before.etag, before.last_modified)


@pytest.mark.req("NF-01")
def test_the_same_invoice_in_two_emails_is_one_file(documents: AzureDocumentStore) -> None:
    first = store_email(documents, fixture("E01_buerobedarf-rechnung"))
    duplicate = store_email(documents, fixture("E15_doppelte-rechnung"))
    assert first.raw.sha256 != duplicate.raw.sha256
    assert first.attachments[0].document == duplicate.attachments[0].document


@pytest.mark.req("NF-01")
def test_a_changed_file_is_refused_on_read_and_never_overwritten(
    documents: AzureDocumentStore, blob_connection_string: str
) -> None:
    # Production has a locked retention policy, so nobody can do this there. Azurite has no
    # lock, which lets the test change a file behind the store's back and see it noticed.
    raw = b"%PDF-1.4 synthetic invoice for the tamper test\n"
    stored = documents.put(raw, "application/pdf")
    blob = blob_client(documents, blob_connection_string, stored.location)
    blob.upload_blob(b"%PDF-1.4 changed amount\n", overwrite=True)

    with pytest.raises(FingerprintMismatch):
        documents.get(stored.sha256)
    with pytest.raises(FingerprintMismatch):
        documents.put(raw, "application/pdf")
    assert blob.download_blob().readall() == b"%PDF-1.4 changed amount\n"


@pytest.mark.req("NF-01")
def test_a_missing_file_is_reported(documents: AzureDocumentStore) -> None:
    with pytest.raises(DocumentNotFound):
        documents.get("0" * 64)


def test_a_location_needs_a_real_fingerprint() -> None:
    with pytest.raises(ValueError, match="64 lower case hex"):
        location_of("../../etc/passwd")


# --- AC: the fingerprint is recorded on the case and in the audit log ---


def stored_and_recorded(
    app: Engine, documents: AzureDocumentStore, name: str
) -> tuple[int, list[int]]:
    email_id = next(_email_ids)
    stored = store_email(documents, fixture(name))
    with unit_of_work(app, INTAKE) as db:
        return email_id, record_email_documents(db, email_id, stored)


@pytest.mark.req("NF-01")
def test_storing_writes_the_fingerprint_into_the_audit_log(
    app: Engine, documents: AzureDocumentStore
) -> None:
    raw = fixture("E08_cloud-hosting-xrechnung")
    email_id, ids = stored_and_recorded(app, documents, "E08_cloud-hosting-xrechnung")

    with app.connect() as db:
        events = db.execute(
            text(
                "select event_type, actor_type, actor, details from audit_event"
                " where email_id = :e and event_type = 'document_stored' order by chain_seq"
            ),
            {"e": email_id},
        ).all()
    assert [e.details["document_id"] for e in events] == ids
    assert [e.details["kind"] for e in events] == ["email", "attachment"]
    assert events[0].details["sha256"] == hashlib.sha256(raw).hexdigest()
    assert events[0].details["location"] == f"sha256/{events[0].details['sha256']}"
    assert {(e.actor_type, e.actor) for e in events} == {("system", "intake")}
    # File names can hold personal names, so they stay out of the ten year log (NF-03).
    assert all("filename" not in e.details for e in events)


@pytest.mark.req("NF-01")
def test_linking_records_the_fingerprint_on_the_case_and_in_its_trace(
    app: Engine, documents: AzureDocumentStore
) -> None:
    email_id, (raw_id, xml_id) = stored_and_recorded(app, documents, "E08_cloud-hosting-xrechnung")
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(db, CaseKind.INCOMING_INVOICE, source_email_id=email_id)
        link_document_to_case(db, case_id, raw_id)
        link_document_to_case(db, case_id, xml_id)
        link_document_to_case(db, case_id, xml_id)  # safe to repeat

    with app.connect() as db:
        on_case = case_documents(db, case_id)
        events = trace(db, case_id)
    assert [(d.document_id, d.kind) for d in on_case] == [
        (raw_id, "email"),
        (xml_id, "attachment"),
    ]
    xml = on_case[1]
    assert xml.filename == "Rechnung_BCH-26-10-0057_xrechnung.xml"
    assert documents.get(xml.sha256) == attachment_parts(fixture("E08_cloud-hosting-xrechnung"))[
        0
    ].get_payload(decode=True)
    linked = [e for e in events if e.event_type == "document_linked"]
    assert [(e.case_id, e.details["sha256"]) for e in linked] == [
        (case_id, on_case[0].sha256),
        (case_id, xml.sha256),
    ]
    stored_events = [e for e in events if e.event_type == "document_stored"]
    assert {e.details["sha256"] for e in stored_events} == {on_case[0].sha256, xml.sha256}


@pytest.mark.req("NF-01")
def test_recording_the_same_email_again_adds_nothing(
    app: Engine, documents: AzureDocumentStore
) -> None:
    raw = fixture("E14_drei-rechnungen-eine-mail")
    email_id, ids = stored_and_recorded(app, documents, "E14_drei-rechnungen-eine-mail")
    with unit_of_work(app, INTAKE) as db:
        again = record_email_documents(db, email_id, store_email(documents, raw))
    assert again == ids
    assert len(ids) == 4
    with app.connect() as db:
        count: int = db.execute(
            text("select count(*) from audit_event where email_id = :e"), {"e": email_id}
        ).scalar_one()
    assert count == 4


@pytest.mark.req("NF-01")
def test_an_email_keeps_its_one_raw_file(app: Engine, documents: AzureDocumentStore) -> None:
    email_id, _ = stored_and_recorded(app, documents, "E01_buerobedarf-rechnung")
    other = documents.put(fixture("E02_cloud-hosting-rechnung"), EML_CONTENT_TYPE)
    with pytest.raises(DuplicateRefused), unit_of_work(app, INTAKE) as db:
        record_document(db, email_id, "email", other)


@pytest.mark.req("NF-01")
def test_a_case_only_takes_files_from_its_own_email(
    app: Engine, documents: AzureDocumentStore
) -> None:
    _, (other_raw, _) = stored_and_recorded(app, documents, "E02_cloud-hosting-rechnung")
    email_id, _ = stored_and_recorded(app, documents, "E01_buerobedarf-rechnung")
    with unit_of_work(app, INTAKE) as db:
        case_id = create_case(db, CaseKind.INCOMING_INVOICE, source_email_id=email_id)
    with (
        pytest.raises(CaseRuleBroken, match="did not come with the email"),
        unit_of_work(app, INTAKE) as db,
    ):
        link_document_to_case(db, case_id, other_raw)


@pytest.mark.req("NF-01")
def test_a_closed_case_takes_no_new_files(app: Engine, documents: AzureDocumentStore) -> None:
    email_id, (raw_id, _) = stored_and_recorded(app, documents, "E01_buerobedarf-rechnung")
    with unit_of_work(app, INTAKE) as db:
        # A duplicate key, as intake sets it (#31): no approval without one.
        case_id = create_case(
            db,
            CaseKind.INCOMING_INVOICE,
            source_email_id=email_id,
            duplicate_key=f"vat:DE700000000|DOCTEST|{email_id}|EUR",
        )
        move_case(db, case_id, expected=CaseStatus.RECEIVED, to=CaseStatus.AWAITING_APPROVAL)
        move_case(
            db,
            case_id,
            expected=CaseStatus.AWAITING_APPROVAL,
            to=CaseStatus.CLOSED,
            closed_reason=ClosedReason.REJECTED,
        )
    with (
        pytest.raises(CaseRuleBroken, match="is closed"),
        unit_of_work(app, INTAKE) as db,
    ):
        link_document_to_case(db, case_id, raw_id)
    with app.connect() as db:
        assert case_documents(db, case_id) == []


# --- The records cannot be changed, and the audit events cannot be faked ---


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "sql",
    [
        "update stored_document set sha256 = repeat('0', 64) where id = :id",
        "update stored_document set filename = 'renamed.pdf' where id = :id",
        "delete from stored_document where id = :id",
        "delete from case_document where document_id = :id",
    ],
)
def test_the_application_cannot_change_or_delete_a_record(
    app: Engine, documents: AzureDocumentStore, sql: str
) -> None:
    _, (raw_id, _) = stored_and_recorded(app, documents, "E01_buerobedarf-rechnung")
    with pytest.raises(NotAllowed, match="permission denied"), unit_of_work(app, INTAKE) as db:
        db.execute(text(sql), {"id": raw_id})


@pytest.mark.req("NF-01")
def test_not_even_the_owner_can_change_a_record(
    owner: Engine, app: Engine, documents: AzureDocumentStore
) -> None:
    _, (raw_id, _) = stored_and_recorded(app, documents, "E01_buerobedarf-rechnung")
    with pytest.raises(CaseRuleBroken, match="append only"), unit_of_work(owner, INTAKE) as db:
        db.execute(text("update stored_document set size_bytes = 1 where id = :id"), {"id": raw_id})
    with pytest.raises(CaseRuleBroken, match="append only"), unit_of_work(owner, INTAKE) as db:
        db.execute(text("delete from stored_document where id = :id"), {"id": raw_id})


@pytest.mark.req("NF-01")
def test_the_database_sets_the_time_not_the_application(app: Engine) -> None:
    with pytest.raises(NotAllowed, match="permission denied"), unit_of_work(app, INTAKE) as db:
        db.execute(
            text(
                "insert into stored_document (email_id, kind, sha256, location, size_bytes,"
                " content_type, stored_at) values (1, 'email', repeat('a', 64),"
                " 'sha256/' || repeat('a', 64), 1, 'message/rfc822', '2020-01-01')"
            )
        )


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    ("sha256", "location"),
    [
        ("A" * 64, "sha256/" + "A" * 64),
        ("a" * 63, "sha256/" + "a" * 63),
        ("a" * 64, "sha256/" + "b" * 64),
        ("a" * 64, "elsewhere/" + "a" * 64),
    ],
)
def test_a_record_needs_a_fingerprint_and_its_location(
    app: Engine, sha256: str, location: str
) -> None:
    with pytest.raises(CaseRuleBroken), unit_of_work(app, INTAKE) as db:
        db.execute(
            text(
                "insert into stored_document (email_id, kind, sha256, location, size_bytes,"
                " content_type) values (1, 'email', :sha256, :location, 1, 'message/rfc822')"
            ),
            {"sha256": sha256, "location": location},
        )


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("event_type", ["document_stored", "document_linked"])
def test_the_application_cannot_write_a_document_event_itself(app: Engine, event_type: str) -> None:
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, INTAKE) as db,
    ):
        record_event(db, event_type, email_id=1, details={"sha256": "0" * 64, "location": "fake"})


@pytest.mark.req("NF-01")
def test_a_record_without_an_actor_is_refused(app: Engine) -> None:
    with pytest.raises(DBAPIError, match=r"argus\.actor is not set"), app.begin() as db:
        db.execute(
            text(
                "insert into stored_document (email_id, kind, sha256, location, size_bytes,"
                " content_type) values (1, 'email', repeat('c', 64),"
                " 'sha256/' || repeat('c', 64), 1, 'message/rfc822')"
            )
        )
