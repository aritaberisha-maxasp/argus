"""A fake Microsoft Entra ID for development and CI (#23, ADR 0007).

It signs access tokens shaped like Entra v2 tokens for the ARGUS API, with its own RSA key,
and publishes the public key the way Entra does. ARGUS checks these tokens with exactly the
code it uses in Azure; only ``ARGUS_ENTRA_AUTHORITY`` points here instead of
login.microsoftonline.com, and ARGUS accepts that only for localhost.

Every ID below is made up. The key is new each time the fake starts and never leaves memory.
"""

import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from pydantic import BaseModel

from argus.auth.roles import Role
from argus.auth.tokens import DEFAULT_SCOPE, EntraSettings, TokenVerifier

AUTHORITY = "http://localhost:8092"
TENANT_ID = "00000000-0000-4000-8000-00000000a7e1"
API_CLIENT_ID = "00000000-0000-4000-8000-00000000a9a1"
ROLE_GROUPS: Mapping[Role, str] = {
    Role.APPROVER: "00000000-0000-4000-8000-0000000000a1",
    Role.FINANCE: "00000000-0000-4000-8000-0000000000f1",
    Role.ADMIN: "00000000-0000-4000-8000-0000000000ad",
}


class FakeEntra:
    def __init__(
        self,
        authority: str = AUTHORITY,
        tenant_id: str = TENANT_ID,
        api_client_id: str = API_CLIENT_ID,
        role_groups: Mapping[Role, str] = ROLE_GROUPS,
    ) -> None:
        self.settings = EntraSettings(
            tenant_id=tenant_id,
            api_client_id=api_client_id,
            role_groups=dict(role_groups),
            authority=authority,
        )
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_id = uuid.uuid4().hex

    def env(self) -> dict[str, str]:
        """The settings ARGUS needs to trust this fake, as environment variables."""
        s = self.settings
        return {
            "ARGUS_ENTRA_AUTHORITY": s.authority,
            "ARGUS_ENTRA_TENANT_ID": s.tenant_id,
            "ARGUS_ENTRA_API_CLIENT_ID": s.api_client_id,
            "ARGUS_ENTRA_GROUP_APPROVER": s.role_groups[Role.APPROVER],
            "ARGUS_ENTRA_GROUP_FINANCE": s.role_groups[Role.FINANCE],
            "ARGUS_ENTRA_GROUP_ADMIN": s.role_groups[Role.ADMIN],
        }

    def public_key_for(self, token: str) -> Any:
        """Stands in for the published key lookup, without HTTP."""
        if jwt.get_unverified_header(token).get("kid") != self.key_id:
            raise jwt.InvalidTokenError("unknown key ID")
        return self._key.public_key()

    def verifier(self) -> TokenVerifier:
        return TokenVerifier(self.settings, keys=self.public_key_for)

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.key_id, "use": "sig", "alg": "RS256"}]}

    def claims(
        self,
        object_id: str,
        *,
        roles: Iterable[Role] = (),
        name: str = "Test User",
        scope: str = DEFAULT_SCOPE,
        lifetime: int = 3600,
    ) -> dict[str, Any]:
        now = int(time.time())
        s = self.settings
        return {
            "iss": s.issuer,
            "aud": s.api_client_id,
            "tid": s.tenant_id,
            "oid": object_id,
            "sub": f"sub-{object_id}",
            "name": name,
            "scp": scope,
            "groups": [s.role_groups[role] for role in roles],
            "iat": now,
            "nbf": now,
            "exp": now + lifetime,
            "ver": "2.0",
        }

    def sign(self, claims: Mapping[str, Any], *, key_id: str | None = None) -> str:
        return jwt.encode(
            dict(claims), self._key, algorithm="RS256", headers={"kid": key_id or self.key_id}
        )

    def token(self, object_id: str, *, roles: Iterable[Role] = (), **options: Any) -> str:
        """A valid token for a person with these roles."""
        return self.sign(self.claims(object_id, roles=roles, **options))


class TokenRequest(BaseModel):
    object_id: str
    roles: list[Role] = []
    name: str = "Test User"


def create_app(fake: FakeEntra | None = None) -> FastAPI:
    """Serves the published keys like Entra, plus a control call that hands out tokens."""
    fake = fake or FakeEntra()
    tenant = fake.settings.tenant_id
    app = FastAPI(title="Fake Entra ID")
    app.state.fake = fake

    @app.get(f"/{tenant}/discovery/v2.0/keys")
    def keys() -> dict[str, Any]:
        return fake.jwks()

    @app.get(f"/{tenant}/v2.0/.well-known/openid-configuration")
    def openid_configuration() -> dict[str, Any]:
        return {
            "issuer": fake.settings.issuer,
            "jwks_uri": fake.settings.jwks_url,
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    @app.post("/_standin/token")
    def token(request: TokenRequest) -> dict[str, str]:
        return {
            "access_token": fake.token(request.object_id, roles=request.roles, name=request.name),
            "token_type": "Bearer",
        }

    @app.get("/_standin/env")
    def env() -> dict[str, str]:
        return fake.env()

    return app


app = create_app()
