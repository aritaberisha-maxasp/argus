"""Checking Microsoft Entra ID access tokens (NF-03, ADR 0008).

Approvers, Benjamin and Arita sign in with their Microsoft 365 account. The page they use gets
an access token for the ARGUS API from Entra and sends it with every request. ARGUS checks the
token itself: the signature against the keys Entra publishes for the tenant, then issuer,
audience, expiry, tenant and the delegated scope. Roles come from the ``groups`` claim.

Locally and in CI a fake identity provider (``standins/entra``) signs tokens with the same
claims, and ARGUS checks them with exactly the same code.
"""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import jwt

from argus.auth.roles import Role, SignedInUser, roles_from_groups

log = logging.getLogger(__name__)

LOGIN_HOST = "login.microsoftonline.com"
# The fake identity provider runs here. No other plain http address is accepted.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
DEFAULT_AUTHORITY = f"https://{LOGIN_HOST}"
DEFAULT_SCOPE = "access_as_user"
# Clocks drift a little between Entra and the app.
LEEWAY_SECONDS = 60
# A token naming a key ID ARGUS does not know makes it fetch Entra's keys again, at most once
# in this many seconds. Without the pause, forged tokens could make ARGUS fetch on every request.
KEY_REFETCH_COOLDOWN_SECONDS = 30

TENANT_ENV = "ARGUS_ENTRA_TENANT_ID"
CLIENT_ENV = "ARGUS_ENTRA_API_CLIENT_ID"
AUTHORITY_ENV = "ARGUS_ENTRA_AUTHORITY"
GROUP_ENV = {
    Role.APPROVER: "ARGUS_ENTRA_GROUP_APPROVER",
    Role.FINANCE: "ARGUS_ENTRA_GROUP_FINANCE",
    Role.ADMIN: "ARGUS_ENTRA_GROUP_ADMIN",
}

# A token for a person always carries these. App only tokens have no scp and are refused.
REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "scp"]


class SignInNotSetUp(RuntimeError):
    """A sign in setting is missing or wrong. The message names the setting, never a value."""


class SignInUnavailable(Exception):
    """ARGUS cannot check tokens right now, because Entra's published keys cannot be fetched.
    The token may be fine, so the caller gets 503 and should try again, not sign in again."""


class SignInRefused(Exception):
    """The token is missing, broken, expired, forged or not meant for ARGUS. The reason goes
    to the log only; the caller just gets 401."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _check_authority(authority: str) -> str:
    parts = urlsplit(authority)
    host = parts.hostname or ""
    if host == LOGIN_HOST and parts.scheme == "https" and parts.path in ("", "/"):
        return authority.rstrip("/")
    if host in LOCAL_HOSTS and parts.scheme == "http" and parts.path in ("", "/"):
        return authority.rstrip("/")
    raise SignInNotSetUp(
        f"{AUTHORITY_ENV} must be {DEFAULT_AUTHORITY} or the local fake on localhost"
    )


@dataclass(frozen=True)
class EntraSettings:
    """Where tokens come from and which groups give which role. None of it is secret."""

    tenant_id: str
    api_client_id: str
    role_groups: Mapping[Role, str]
    authority: str = DEFAULT_AUTHORITY
    scope: str = DEFAULT_SCOPE
    issuer: str = field(init=False)
    jwks_url: str = field(init=False)

    def __post_init__(self) -> None:
        authority = _check_authority(self.authority)
        if not self.tenant_id.strip() or not self.api_client_id.strip():
            raise SignInNotSetUp("the tenant ID and the API client ID must not be empty")
        missing = [role.value for role in Role if not self.role_groups.get(role, "").strip()]
        if missing:
            raise SignInNotSetUp(f"no Entra group for the role(s): {', '.join(missing)}")
        if len(set(self.role_groups.values())) != len(self.role_groups):
            # One group for two roles would quietly give every approver finance or admin rights.
            raise SignInNotSetUp("each role needs its own Entra group")
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "issuer", f"{authority}/{self.tenant_id}/v2.0")
        object.__setattr__(self, "jwks_url", f"{authority}/{self.tenant_id}/discovery/v2.0/keys")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EntraSettings":
        environ = os.environ if environ is None else environ
        names = [TENANT_ENV, CLIENT_ENV, *GROUP_ENV.values()]
        missing = [name for name in names if not environ.get(name, "").strip()]
        if missing:
            raise SignInNotSetUp(f"not set: {', '.join(missing)}")
        return cls(
            tenant_id=environ[TENANT_ENV].strip(),
            api_client_id=environ[CLIENT_ENV].strip(),
            role_groups={role: environ[name].strip() for role, name in GROUP_ENV.items()},
            authority=environ.get(AUTHORITY_ENV, "").strip() or DEFAULT_AUTHORITY,
        )


# Finds the public key that should have signed a token, by the key ID in its header.
KeyResolver = Callable[[str], Any]


def published_keys(settings: EntraSettings) -> KeyResolver:
    """The keys Entra publishes for the tenant. They are cached, and fetched again when a token
    names a key that is not in the cache yet (Entra rolls its keys now and then), but at most
    once per cooldown. PyJWT has the cooldown from 2.14 on, hence the floor in pyproject.toml."""
    client = jwt.PyJWKClient(
        settings.jwks_url,
        cache_keys=True,
        lifespan=3600,
        timeout=10,
        cooldown_duration=KEY_REFETCH_COOLDOWN_SECONDS,
    )

    def key_for(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return key_for


class TokenVerifier:
    def __init__(self, settings: EntraSettings, keys: KeyResolver | None = None) -> None:
        self.settings = settings
        self._keys = keys or published_keys(settings)

    def verify(self, token: str) -> SignedInUser:
        """The person the token belongs to, with their ARGUS roles. Raises ``SignInRefused``,
        or ``SignInUnavailable`` when Entra's keys cannot be fetched."""
        settings = self.settings
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._keys(token),
                algorithms=["RS256"],
                audience=settings.api_client_id,
                issuer=settings.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.PyJWKClientConnectionError as error:
            raise SignInUnavailable("Entra's published keys cannot be fetched") from error
        except jwt.PyJWTError as error:
            raise SignInRefused(f"token check failed: {type(error).__name__}") from error

        if claims["tid"] != settings.tenant_id:
            raise SignInRefused("token is from another tenant")
        if settings.scope not in str(claims["scp"]).split():
            raise SignInRefused(f"token lacks the scope {settings.scope}")
        if "groups" in claims.get("_claim_names", {}) or claims.get("hasgroups"):
            # Entra leaves the groups out when someone is in too many. ARGUS cannot tell the
            # roles then, so it gives none. docs/operations/sign-in.md shows how to avoid this.
            raise SignInRefused("token has too many groups to list them")
        groups = claims.get("groups", [])
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise SignInRefused("groups claim is not a list of IDs")
        object_id = claims["oid"]
        if not isinstance(object_id, str) or not object_id.strip():
            raise SignInRefused("token has no object ID")
        name = claims.get("name") or claims.get("preferred_username") or ""
        address = claims.get("preferred_username")
        return SignedInUser(
            object_id=object_id,
            name=str(name),
            roles=roles_from_groups(groups, settings.role_groups),
            sign_in_address=address if isinstance(address, str) and address.strip() else None,
        )


def verifier_from_env() -> TokenVerifier | None:
    """The verifier for the settings in the environment, or None when sign in is not set up.
    Without one, every route with data answers 503, so a missing setting never opens ARGUS."""
    try:
        return TokenVerifier(EntraSettings.from_env())
    except SignInNotSetUp as problem:
        log.warning("sign in is not set up, so every route with data is closed: %s", problem)
        return None
