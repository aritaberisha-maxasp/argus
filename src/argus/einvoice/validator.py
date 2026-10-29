"""Check an e-invoice with the official KoSIT validator (#28, NF-02, ADR 0006 point 1).

The KoSIT validator is the reference check for XRechnung, run by the coordination office of the
German public sector. It runs as its own container next to ARGUS (``docker/kosit-validator``) with
the official XRechnung configuration, which also holds the EN 16931 rules for ZUGFeRD and
Factur-X. ARGUS posts the XML to it and reads the report it sends back.

An e-invoice the validator does not accept goes to a person with the rules it broke. When the
validator cannot be reached, :class:`ValidatorUnavailable` is raised and the email is tried again
in the next sweep, so a validator outage never lets an unchecked e-invoice through.
"""

import logging
from dataclasses import dataclass
from typing import Protocol
from xml.etree.ElementTree import Element

import httpx

from argus.einvoice.safe_xml import XmlRefused, parse

log = logging.getLogger("argus.einvoice")

REPORT = "{http://www.xoev.de/de/validator/varl/1}"
SCENARIOS = "{http://www.xoev.de/de/validator/framework/1/scenarios}"


class ValidatorUnavailable(RuntimeError):
    """The validator could not be reached or gave no usable answer. Try again later."""


@dataclass(frozen=True)
class ValidationMessage:
    level: str  # error, warning or information
    code: str  # the rule, for example BR-DE-15
    text: str


@dataclass(frozen=True)
class Validation:
    accepted: bool
    scenario: str | None  # the rule set the validator used; None when it had none for this XML
    messages: tuple[ValidationMessage, ...] = ()

    @property
    def errors(self) -> tuple[ValidationMessage, ...]:
        return tuple(m for m in self.messages if m.level in ("error", "fatal"))


class Validator(Protocol):
    def validate(self, xml: bytes) -> Validation: ...


class KositValidator:
    """Talks to the KoSIT validator in daemon mode: POST the XML, read the report."""

    def __init__(self, url: str, client: httpx.Client | None = None, timeout: float = 60.0):
        self._url = url
        self._client = client or httpx.Client(timeout=timeout)

    def validate(self, xml: bytes) -> Validation:
        try:
            response = self._client.post(
                self._url, content=xml, headers={"Content-Type": "application/xml"}
            )
        except httpx.HTTPError as error:
            raise ValidatorUnavailable(f"the validator cannot be reached: {error}") from error
        # 200 means accepted, 406 not accepted and 422 not well formed; all carry the report.
        if response.status_code not in (200, 406, 422):
            raise ValidatorUnavailable(f"the validator answered HTTP {response.status_code}")
        validation = read_report(response.content)
        log.info(
            "The validator %s an e-invoice with %d errors",
            "accepted" if validation.accepted else "did not accept",
            len(validation.errors),
        )
        return validation


def read_report(report: bytes) -> Validation:
    """Read a KoSIT validation report (VARL)."""
    try:
        root = parse(report)
    except XmlRefused as error:
        raise ValidatorUnavailable(f"the validator report cannot be read: {error}") from error
    if root.tag != f"{REPORT}report":
        raise ValidatorUnavailable("the validator answered with something that is not a report")
    assessment = root.find(f"{REPORT}assessment")
    accepted = assessment is not None and assessment.find(f"{REPORT}accept") is not None
    if assessment is None or (not accepted and assessment.find(f"{REPORT}reject") is None):
        raise ValidatorUnavailable("the validator report has no assessment")
    name = root.find(f"{REPORT}scenarioMatched/{SCENARIOS}scenario/{SCENARIOS}name")
    messages = tuple(_message(m) for m in root.iter(f"{REPORT}message"))
    return Validation(
        accepted=accepted,
        scenario="".join(name.itertext()).strip() if name is not None else None,
        messages=messages,
    )


def _message(node: Element) -> ValidationMessage:
    return ValidationMessage(
        level=node.get("level", "error"),
        code=node.get("code", ""),
        text=" ".join("".join(node.itertext()).split()),
    )
