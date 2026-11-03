"""Does the email really come from the domain in its From address? (ADR 0006 point 4)

The From address is easy to fake. Exchange Online checks SPF, DKIM and DMARC for every email that
reaches Max ASP from the internet and writes what it found into an ``Authentication-Results``
header on top of the email. ARGUS counts the From domain as checked only when that header shows
one of these for exactly that domain:

- **DMARC passes** for the From domain (``header.from``). DMARC itself only passes when SPF or
  DKIM passed for a domain that belongs to the From domain.
- **DKIM passes** with a signature of the From domain (``header.d``).
- **SPF passes** for an envelope sender (``smtp.mailfrom``) in the From domain. A plain SPF pass
  is not enough, because SPF checks the envelope sender and not the From address: anyone can
  pass SPF for a domain of their own and still write any From address they like.

Microsoft writes ``dmarc=bestguesspass`` when the domain has no DMARC policy at all. That is not a
pass. A DKIM or SPF pass for a sub or parent domain is not enough on its own either; DMARC is
what decides whether such a domain belongs to the From domain.

**Only the topmost header counts.** Headers are added on top as an email travels, so the topmost
``Authentication-Results`` is the one Exchange Online wrote. Any further down was in the email
before it reached Max ASP, and a sender can write anything there.

Who a checked domain belongs to (the invoicing agent, a vendor, a customer) is decided in
:mod:`argus.sorting.checks`. Covers IN-04, IN-09, AR-02.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

HEADER = "authentication-results"
METHODS = ("dmarc", "dkim", "spf")

# A comment in parentheses, as Microsoft writes them: "(sender IP is 192.0.2.10)".
_COMMENT = re.compile(r"\([^()]*\)")
# A separator, or a run of anything else in which a quoted part may hold spaces or separators.
_TOKEN = re.compile(r'(?:[^\s;"]+|"[^"]*")+|;')


def first_headers(items: Iterable[tuple[str, Any]]) -> dict[str, str]:
    """Header names in lower case with the value of their topmost copy.

    The topmost copy is the newest one. For ``Authentication-Results`` that is the copy Exchange
    Online wrote; a copy further down came with the email and could be written by the sender."""
    headers: dict[str, str] = {}
    for key, value in items:
        headers.setdefault(key.lower(), str(value))
    return headers


def domain_of(value: str) -> str:
    """The domain of an address or a domain, in lower case: "Rechnung@Muster.example" gives
    "muster.example"."""
    return value.strip().strip('"').rpartition("@")[2].strip().rstrip(".").lower()


@dataclass(frozen=True)
class MethodResult:
    method: str  # "spf", "dkim", "dmarc", or anything else Exchange adds, such as "compauth"
    result: str  # "pass", "fail", "none", "bestguesspass" and so on
    properties: Mapping[str, str]  # "smtp.mailfrom", "header.d", "header.from"


def parse_results(value: str) -> tuple[MethodResult, ...]:
    """The results in one ``Authentication-Results`` header (RFC 8601).

    Microsoft leaves out the name of the checking server and starts with the first result;
    other servers start with their name. Both are read."""
    text = value
    while (stripped := _COMMENT.sub(" ", text)) != text:
        text = stripped  # comments can hold comments
    results: list[MethodResult] = []
    part: list[str] = []
    for piece in [*_TOKEN.findall(text), ";"]:
        if piece != ";":
            part.append(piece)
            continue
        if part and "=" in part[0] and not part[0].startswith('"'):
            method, _, result = part[0].partition("=")
            properties: dict[str, str] = {}
            for item in part[1:]:
                key, equals, item_value = item.partition("=")
                if equals:
                    properties.setdefault(key.lower(), item_value.strip('"'))
            results.append(MethodResult(method.lower(), result.lower(), properties))
        part = []
    return tuple(results)


@dataclass(frozen=True)
class Authentication:
    """What the topmost ``Authentication-Results`` header says about the From domain."""

    from_domain: str
    passed_by: str | None  # "dmarc", "dkim" or "spf"; None when nothing passed for the domain
    results: tuple[str, ...]  # one line per method, domains only: "spf=pass other.example"

    @property
    def passed(self) -> bool:
        return self.passed_by is not None


def authenticate(sender_address: str, headers: Mapping[str, str]) -> Authentication:
    """Check the From domain of ``sender_address`` against the topmost ``Authentication-Results``.

    ``headers`` must hold the topmost copy of each header (see :func:`first_headers`)."""
    from_domain = domain_of(sender_address) if "@" in sender_address else ""
    found = parse_results(headers.get(HEADER, ""))
    passes: set[str] = set()
    lines: list[str] = []
    for item in found:
        if item.method not in METHODS:
            continue
        key = {"dmarc": "header.from", "dkim": "header.d", "spf": "smtp.mailfrom"}[item.method]
        domain = domain_of(item.properties.get(key, ""))
        # Only the domain, never the part before the @, which can name a person (NF-03).
        lines.append(f"{item.method}={item.result} {domain or '(no domain)'}")
        if item.result == "pass" and from_domain and domain == from_domain:
            passes.add(item.method)
    passed_by = next((method for method in METHODS if method in passes), None)
    if not found:
        lines.append("no Authentication-Results header from Exchange Online")
    return Authentication(from_domain, passed_by, tuple(lines))


# Who the From address says the sender is, and who ARGUS treats it as after the check.
INVOICING_AGENT = "invoicing_agent"
INTERNAL = "internal"
KNOWN_VENDOR = "known_vendor"
KNOWN_CUSTOMER = "known_customer"
UNKNOWN = "unknown"
NEEDS_CHECK = (INVOICING_AGENT, KNOWN_VENDOR, KNOWN_CUSTOMER)

DESCRIBED = {
    INVOICING_AGENT: "the invoicing agent",
    KNOWN_VENDOR: "a known vendor",
    KNOWN_CUSTOMER: "a known customer",
}


@dataclass(frozen=True)
class SenderCheck:
    """The result that is kept with the email and in the audit log (#70)."""

    claimed: str  # who the From address says: invoicing_agent, known_vendor and so on
    treated_as: str  # the same when the check passed, otherwise unknown
    authentication: Authentication

    @property
    def failed_claim(self) -> bool:
        """The From address names the invoicing agent or a known partner, but the email does
        not really come from that domain."""
        return self.claimed != self.treated_as

    def reason(self) -> str:
        return (
            f"the sender claims to be {DESCRIBED.get(self.claimed, self.claimed)} but SPF, DKIM "
            f"and DMARC do not pass for {self.authentication.from_domain or 'its domain'}"
        )

    def audit_details(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "treated_as": self.treated_as,
            "from_domain": self.authentication.from_domain,
            "authenticated": self.authentication.passed,
            "passed_by": self.authentication.passed_by,
            "results": list(self.authentication.results),
        }

    @classmethod
    def from_audit_details(cls, details: Mapping[str, Any]) -> "SenderCheck":
        return cls(
            claimed=details["claimed"],
            treated_as=details["treated_as"],
            authentication=Authentication(
                from_domain=details["from_domain"],
                passed_by=details.get("passed_by"),
                results=tuple(details.get("results") or ()),
            ),
        )
