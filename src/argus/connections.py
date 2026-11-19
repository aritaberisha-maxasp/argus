"""Where ARGUS finds Microsoft 365, DATEV and Wise: the real systems or the demo stand ins (#124).

ARGUS is pointed at the outside systems by settings only, so moving from the demo to the real
systems is a change of settings, not of code (ADR 0009):

- ``ARGUS_DEMO``: ``true`` while ARGUS runs against the stand ins, ``false`` or unset otherwise.
- ``ARGUS_GRAPH_URL`` and ``ARGUS_MAILBOX``: Microsoft Graph and the accounts mailbox.
- ``ARGUS_REMINDER_FORWARD_TO`` and ``ARGUS_DIGEST_TO``: the one address payment reminders are
  forwarded to (#27) and the one address the daily digest of open exceptions goes to (#61).
- ``ARGUS_DATEV_URL`` and ``ARGUS_DATEV_CLIENT``: DATEVconnect and the DATEV client (Mandant).
- ``ARGUS_WISE_URL`` and ``ARGUS_WISE_PROFILE_ID``: Wise and the Max ASP profile.
- ``ARGUS_GRAPH_STANDIN_TOKEN`` and ``ARGUS_WISE_STANDIN_TOKEN``: the tokens of the stand ins.
  They open nothing but the stand ins.

The two worlds never mix. :func:`connections_from_env` refuses, and with it the app refuses to
start, when demo mode is on and a real address or a real secret is also set, or when demo mode
is off and a stand in address or token is set. A demo can then never write to the real mailbox,
book into the real DATEV or hand a payment to the real Wise, and a live ARGUS can never quietly
talk to a stand in.

A stand in address is plain http to a host without a dot, the way one container app calls
another inside the Container Apps environment (``http://ca-argus-graph``), or ``localhost``.
A real address is https to a full host name. Error messages name settings, never values.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from argus.datev.datevconnect import DatevConnectGateway
from argus.intake.graph import ALL_CALLS, GraphCall, GraphClient, TokenProvider
from argus.wise.client import TOKEN_SECRET as WISE_TOKEN_SECRET
from argus.wise.client import WiseClient

DEMO_ENV: Final = "ARGUS_DEMO"
GRAPH_URL_ENV: Final = "ARGUS_GRAPH_URL"
MAILBOX_ENV: Final = "ARGUS_MAILBOX"
REMINDER_FORWARD_TO_ENV: Final = "ARGUS_REMINDER_FORWARD_TO"
DIGEST_TO_ENV: Final = "ARGUS_DIGEST_TO"
DATEV_URL_ENV: Final = "ARGUS_DATEV_URL"
DATEV_CLIENT_ENV: Final = "ARGUS_DATEV_CLIENT"
WISE_URL_ENV: Final = "ARGUS_WISE_URL"
WISE_PROFILE_ENV: Final = "ARGUS_WISE_PROFILE_ID"
GRAPH_STANDIN_TOKEN_ENV: Final = "ARGUS_GRAPH_STANDIN_TOKEN"  # noqa: S105  a name, no value
WISE_STANDIN_TOKEN_ENV: Final = "ARGUS_WISE_STANDIN_TOKEN"  # noqa: S105  a name, no value

ADDRESSES: Final = (GRAPH_URL_ENV, DATEV_URL_ENV, WISE_URL_ENV)
STANDIN_SECRETS: Final = (GRAPH_STANDIN_TOKEN_ENV, WISE_STANDIN_TOKEN_ENV)
# Secrets of the real systems. Graph has none today, since ARGUS signs in to Microsoft 365 with
# its managed identity (ADR 0003); the Graph and DATEV names are kept here so that whoever
# brings in such a secret cannot forget the demo rule. Add every new real secret to this list.
REAL_SECRETS: Final = (
    WISE_TOKEN_SECRET,
    "ARGUS_GRAPH_CLIENT_SECRET",
    "AZURE_CLIENT_SECRET",
    "ARGUS_DATEV_CLIENT_SECRET",
    "ARGUS_DATEV_API_KEY",
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off", ""})


class ConnectionSettingsError(RuntimeError):
    """The settings for Microsoft 365, DATEV or Wise break the demo rule or are incomplete. The
    message names the settings, never their values."""


class NotConnected(LookupError):
    """ARGUS has no setting for this system, so it does not talk to it."""


def is_standin_address(url: str) -> bool:
    """Plain http to a host inside the Container Apps environment, or to this machine."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "http" and bool(host) and ("." not in host or host in _LOCAL_HOSTS)


def is_real_address(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and "." in host and host not in _LOCAL_HOSTS


def demo_from_env(environ: Mapping[str, str]) -> bool:
    value = environ.get(DEMO_ENV, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConnectionSettingsError(f"{DEMO_ENV} must be true or false")


class _StaticToken:
    """The stand in's token for the fake Graph. It opens nothing else."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def token(self, scope: str) -> SecretStr:
        return self._token


@dataclass(frozen=True)
class Connections:
    """Checked settings for the outside systems. Build it with :func:`connections_from_env`."""

    demo: bool
    graph_url: str | None
    mailbox: str | None
    reminder_forward_to: str | None
    datev_url: str | None
    datev_client: str | None
    wise_url: str | None
    wise_profile_id: int | None
    graph_standin_token: SecretStr | None
    wise_standin_token: SecretStr | None
    wise_token: SecretStr | None
    digest_to: str | None = None

    def graph_client(
        self,
        tokens: TokenProvider | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        digest: bool = False,
        statement_folder: str | None = None,
        calls: frozenset[GraphCall] = ALL_CALLS,
    ) -> GraphClient:
        """The Graph client for the accounts mailbox. In demo mode it signs in to the fake
        Graph with the stand in token. Live, ``tokens`` signs in to Microsoft 365.
        ``transport`` is for tests.

        Each client may send only what its job sends: the intake client forwards reminders,
        the ``digest=True`` client sends the daily digest and nothing else (#61). Only a client
        given ``statement_folder`` may list the emails of that one folder (#166). ``calls`` are
        the kinds of Graph call the job needs; each job passes its own list (#168)."""
        if not self.graph_url or not self.mailbox:
            raise NotConnected(f"set {GRAPH_URL_ENV} and {MAILBOX_ENV} to read the mailbox")
        if self.demo:
            if self.graph_standin_token is None:
                raise ConnectionSettingsError(f"demo mode needs {GRAPH_STANDIN_TOKEN_ENV}")
            tokens = _StaticToken(self.graph_standin_token)
        elif tokens is None:
            raise NotConnected("the Microsoft 365 sign in for the real mailbox is not set up")
        return GraphClient(
            tokens,
            self.mailbox,
            base_url=self.graph_url,
            transport=transport,
            standin=self.demo,
            forward_to=None if digest else self.reminder_forward_to,
            digest_to=self.digest_to if digest else None,
            statement_folder=None if digest else statement_folder,
            calls=calls,
        )

    def datev_gateway(self) -> DatevConnectGateway:
        """The DATEV gateway. In demo mode it talks to the fake DATEV."""
        if not self.datev_url or not self.datev_client:
            raise NotConnected(f"set {DATEV_URL_ENV} and {DATEV_CLIENT_ENV} to reach DATEV")
        return DatevConnectGateway.from_url(self.datev_url, self.datev_client)

    def wise_client(self, transport: httpx.BaseTransport | None = None) -> WiseClient:
        """The Wise client. In demo mode it talks to the Wise stand in, which like Wise for
        ARGUS has no way to fund or release a payment (NF-04). ``transport`` is for tests."""
        if not self.wise_url or self.wise_profile_id is None:
            raise NotConnected(f"set {WISE_URL_ENV} and {WISE_PROFILE_ENV} to reach Wise")
        token = self.wise_standin_token if self.demo else self.wise_token
        if token is None:
            name = WISE_STANDIN_TOKEN_ENV if self.demo else WISE_TOKEN_SECRET
            raise ConnectionSettingsError(f"set {name} to reach Wise")
        return WiseClient(
            token, self.wise_profile_id, self.wise_url, transport=transport, standin=self.demo
        )


def _value(environ: Mapping[str, str], name: str) -> str | None:
    return environ.get(name, "").strip() or None


def _secret(environ: Mapping[str, str], name: str) -> SecretStr | None:
    value = _value(environ, name)
    return SecretStr(value) if value else None


def demo_rule_problems(environ: Mapping[str, str]) -> list[str]:
    """Why these settings break the demo rule, or an empty list. Names settings, never values."""
    demo = demo_from_env(environ)
    problems = []
    for name in ADDRESSES:
        url = _value(environ, name)
        if url is None:
            continue
        if demo and not is_standin_address(url):
            problems.append(f"demo mode is on but {name} is not a stand in address")
        if not demo and not is_real_address(url):
            problems.append(f"demo mode is off but {name} is not an https address")
    for name in REAL_SECRETS:
        if demo and _value(environ, name):
            problems.append(f"demo mode is on but the real secret {name} is set")
    for name in STANDIN_SECRETS:
        if not demo and _value(environ, name):
            problems.append(f"demo mode is off but the stand in token {name} is set")
    return problems


def connections_from_env(environ: Mapping[str, str] | None = None) -> Connections:
    """Read and check the settings. Raises :class:`ConnectionSettingsError` when they break
    the demo rule, so ARGUS does not start with them."""
    environ = os.environ if environ is None else environ
    problems = demo_rule_problems(environ)
    profile = _value(environ, WISE_PROFILE_ENV)
    if profile is not None and not profile.isdigit():
        problems.append(f"{WISE_PROFILE_ENV} must be a number")
    if problems:
        raise ConnectionSettingsError("; ".join(problems))
    return Connections(
        demo=demo_from_env(environ),
        graph_url=_value(environ, GRAPH_URL_ENV),
        mailbox=_value(environ, MAILBOX_ENV),
        reminder_forward_to=_value(environ, REMINDER_FORWARD_TO_ENV),
        datev_url=_value(environ, DATEV_URL_ENV),
        datev_client=_value(environ, DATEV_CLIENT_ENV),
        wise_url=_value(environ, WISE_URL_ENV),
        wise_profile_id=int(profile) if profile is not None else None,
        graph_standin_token=_secret(environ, GRAPH_STANDIN_TOKEN_ENV),
        wise_standin_token=_secret(environ, WISE_STANDIN_TOKEN_ENV),
        wise_token=_secret(environ, WISE_TOKEN_SECRET),
        digest_to=_value(environ, DIGEST_TO_ENV),
    )
