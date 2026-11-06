"""Sign in with Microsoft Entra ID and the three roles (#23, NF-03, ADR 0007).

The fake identity provider in ``standins/entra`` signs the tokens. ARGUS checks them with the
same code it uses in Azure.
"""

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from argus.app import PUBLIC_PATHS, create_app
from argus.auth import (
    EntraSettings,
    Role,
    SignedInUser,
    SignInNotSetUp,
    SignInRefused,
    install_sign_in,
    may_view_case,
    require_role,
    roles_from_groups,
    verifier_from_env,
)
from standins.entra.app import API_CLIENT_ID, ROLE_GROUPS, TENANT_ID, FakeEntra
from standins.entra.app import create_app as create_fake_entra

pytestmark = pytest.mark.req("NF-03")

ALICE = "11111111-1111-4111-8111-111111111111"  # an approver, made up
BOB = "22222222-2222-4222-8222-222222222222"  # another approver, made up


def no_database() -> Engine:
    raise RuntimeError("these tests have no database")


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(entra: FakeEntra) -> TestClient:
    return TestClient(create_app(no_database, sign_in=entra.verifier))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Roles and the access rule


def test_roles_come_from_the_entra_groups() -> None:
    groups = [ROLE_GROUPS[Role.APPROVER], "some-other-group", ROLE_GROUPS[Role.ADMIN]]

    assert roles_from_groups(groups, ROLE_GROUPS) == {Role.APPROVER, Role.ADMIN}
    assert roles_from_groups(["some-other-group"], ROLE_GROUPS) == frozenset()


@pytest.mark.parametrize(
    ("roles", "approvers", "expected"),
    [
        ({Role.APPROVER}, frozenset({ALICE}), True),
        ({Role.APPROVER}, frozenset({BOB}), False),
        ({Role.APPROVER}, frozenset({BOB, ALICE}), True),  # Alice stands in as deputy
        ({Role.APPROVER}, frozenset(), False),
        ({Role.FINANCE}, frozenset({BOB}), True),
        ({Role.ADMIN}, frozenset(), True),
        (set(), frozenset({ALICE}), False),  # named as approver but not in the approver group
    ],
)
def test_an_approver_sees_only_their_own_cases(
    roles: set[Role], approvers: frozenset[str], expected: bool
) -> None:
    user = SignedInUser(ALICE, "Alice", frozenset(roles))

    assert may_view_case(user, approvers) is expected


# Settings


def test_settings_accept_entra_and_the_local_fake_only() -> None:
    def settings(authority: str) -> EntraSettings:
        return EntraSettings(TENANT_ID, API_CLIENT_ID, ROLE_GROUPS, authority=authority)

    assert settings("https://login.microsoftonline.com").issuer == (
        f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
    )
    assert settings("http://localhost:8092").jwks_url == (
        f"http://localhost:8092/{TENANT_ID}/discovery/v2.0/keys"
    )
    for wrong in (
        "http://login.microsoftonline.com",
        "https://login.microsoftonline.com.evil.example",
        "https://evil.example",
        "http://10.0.0.5:8092",
        "https://login.microsoftonline.com/other-path",
    ):
        with pytest.raises(SignInNotSetUp):
            settings(wrong)


def test_each_role_needs_its_own_group() -> None:
    same = {Role.APPROVER: "g1", Role.FINANCE: "g1", Role.ADMIN: "g2"}
    with pytest.raises(SignInNotSetUp, match="own Entra group"):
        EntraSettings(TENANT_ID, API_CLIENT_ID, same)
    with pytest.raises(SignInNotSetUp, match="admin"):
        EntraSettings(TENANT_ID, API_CLIENT_ID, {Role.APPROVER: "g1", Role.FINANCE: "g2"})


def test_settings_from_env_name_what_is_missing(entra: FakeEntra) -> None:
    env = entra.env()
    assert EntraSettings.from_env(env).role_groups == ROLE_GROUPS

    del env["ARGUS_ENTRA_GROUP_FINANCE"]
    with pytest.raises(SignInNotSetUp, match="ARGUS_ENTRA_GROUP_FINANCE"):
        EntraSettings.from_env(env)


# The token check


def test_a_valid_token_gives_the_person_and_their_roles(entra: FakeEntra) -> None:
    token = entra.token(ALICE, roles=[Role.APPROVER], name="Alice Approver")

    user = entra.verifier().verify(token)

    assert user == SignedInUser(ALICE, "Alice Approver", frozenset({Role.APPROVER}))
    assert str(user.actor) == f"user:{ALICE}"


def other_key_token(entra: FakeEntra, claims: dict[str, Any]) -> str:
    """Signed with a key that is not the fake's, but naming the fake's key ID."""
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return jwt.encode(claims, stranger, algorithm="RS256", headers={"kid": entra.key_id})


def hs256_with_public_key(entra: FakeEntra, claims: dict[str, Any]) -> str:
    """The old trick: sign with HMAC, using the public key as the shared secret."""
    pem = (
        entra.public_key_for(entra.token(ALICE))
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    header = {"alg": "HS256", "typ": "JWT", "kid": entra.key_id}
    signing_input = ".".join(
        jwt.utils.base64url_encode(json.dumps(part).encode()).decode() for part in (header, claims)
    )
    signature = hmac.new(pem.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{jwt.utils.base64url_encode(signature).decode()}"


def unsigned(entra: FakeEntra, claims: dict[str, Any]) -> str:
    header = {"alg": "none", "typ": "JWT", "kid": entra.key_id}
    parts = (jwt.utils.base64url_encode(json.dumps(p).encode()).decode() for p in (header, claims))
    return ".".join(parts) + "."


BAD_TOKENS: dict[str, Callable[[FakeEntra], str]] = {
    "expired": lambda e: e.sign({**e.claims(ALICE), "exp": int(time.time()) - 600}),
    "not yet valid": lambda e: e.sign({**e.claims(ALICE), "nbf": int(time.time()) + 600}),
    "for another app": lambda e: e.sign({**e.claims(ALICE), "aud": "another-app"}),
    "from another issuer": lambda e: e.sign(
        {**e.claims(ALICE), "iss": "https://login.microsoftonline.com/other/v2.0"}
    ),
    "from another tenant": lambda e: e.sign({**e.claims(ALICE), "tid": "another-tenant"}),
    "without the scope": lambda e: e.sign({**e.claims(ALICE), "scp": "User.Read"}),
    "app only, no scope": lambda e: e.sign(
        {k: v for k, v in e.claims(ALICE).items() if k != "scp"}
    ),
    "without object ID": lambda e: e.sign({k: v for k, v in e.claims(ALICE).items() if k != "oid"}),
    "with an empty object ID": lambda e: e.sign({**e.claims(ALICE), "oid": " "}),
    "too many groups": lambda e: e.sign(
        {**e.claims(ALICE), "_claim_names": {"groups": "src1"}, "groups": []}
    ),
    "groups not a list": lambda e: e.sign({**e.claims(ALICE), "groups": "a,b"}),
    "unknown key ID": lambda e: e.sign(e.claims(ALICE), key_id="some-other-key"),
    "signed with another key": lambda e: other_key_token(e, e.claims(ALICE)),
    "HS256 with the public key": lambda e: hs256_with_public_key(e, e.claims(ALICE)),
    "not signed at all": lambda e: unsigned(e, e.claims(ALICE)),
    "not a token": lambda e: "not-a-token",
    "empty": lambda e: "",
}


@pytest.mark.parametrize("case", BAD_TOKENS)
def test_a_bad_token_is_refused(entra: FakeEntra, case: str) -> None:
    token = BAD_TOKENS[case](entra)

    with pytest.raises(SignInRefused):
        entra.verifier().verify(token)


def test_a_token_from_another_fake_is_refused(entra: FakeEntra) -> None:
    stranger = FakeEntra()

    with pytest.raises(SignInRefused):
        entra.verifier().verify(stranger.token(ALICE, roles=[Role.ADMIN]))


# The web app: no data without sign in


def protected_routes(app: FastAPI) -> list[APIRoute]:
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path not in PUBLIC_PATHS]
    assert routes, "the app should have routes with data"
    return routes


def call(client: TestClient, route: APIRoute, headers: dict[str, str] | None = None) -> Any:
    path = route.path.replace("{case_id}", "1")
    assert "{" not in path, f"give {route.path} a sample value in this test"
    return client.request(sorted(route.methods or {"GET"})[0], path, headers=headers)


def test_only_the_health_checks_are_open(client: TestClient) -> None:
    open_routes = {r.path for r in client.app.routes if isinstance(r, APIRoute)}  # type: ignore[attr-defined]

    assert open_routes & PUBLIC_PATHS == PUBLIC_PATHS
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("case", ["no header", "not bearer", *BAD_TOKENS])
def test_a_request_without_a_valid_sign_in_gets_no_data(
    entra: FakeEntra, client: TestClient, case: str
) -> None:
    if case == "no header":
        headers = None
    elif case == "not bearer":
        headers = {"Authorization": "Basic dXNlcg=="}
    else:
        headers = bearer(BAD_TOKENS[case](entra))

    for route in protected_routes(client.app):  # type: ignore[arg-type]
        response = call(client, route, headers)
        assert response.status_code == 401, route.path
        assert response.json() == {"detail": "sign in needed"}, route.path
        assert response.headers["www-authenticate"].startswith("Bearer")


def test_a_person_without_an_argus_role_gets_no_data(entra: FakeEntra, client: TestClient) -> None:
    token = entra.token(ALICE, roles=[])

    for route in protected_routes(client.app):  # type: ignore[arg-type]
        response = call(client, route, bearer(token))
        assert response.status_code == 403, route.path
        assert response.json() == {"detail": "no ARGUS role"}


def test_without_sign_in_settings_every_route_with_data_is_closed(entra: FakeEntra) -> None:
    client = TestClient(create_app(no_database, sign_in=lambda: None))
    token = entra.token(ALICE, roles=[Role.ADMIN])

    for route in protected_routes(client.app):  # type: ignore[arg-type]
        assert call(client, route, bearer(token)).status_code == 503
    assert client.get("/health").status_code == 200


def test_me_names_the_signed_in_person(entra: FakeEntra, client: TestClient) -> None:
    token = entra.token(BOB, roles=[Role.FINANCE, Role.APPROVER], name="Benjamin")

    response = client.get("/me", headers=bearer(token))

    assert response.status_code == 200
    assert response.json() == {
        "object_id": BOB,
        "name": "Benjamin",
        "roles": ["approver", "finance"],
    }


def test_require_role_lets_only_those_roles_in(entra: FakeEntra) -> None:
    app = FastAPI()
    install_sign_in(app, entra.verifier())

    @app.get("/admin-only")
    def admin_only(user: SignedInUser = Depends(require_role(Role.ADMIN))) -> str:  # noqa: B008
        return user.object_id

    client = TestClient(app)
    as_admin = client.get("/admin-only", headers=bearer(entra.token(ALICE, roles=[Role.ADMIN])))
    as_finance = client.get("/admin-only", headers=bearer(entra.token(BOB, roles=[Role.FINANCE])))

    assert as_admin.status_code == 200
    assert as_admin.json() == ALICE
    assert as_finance.status_code == 403
    with pytest.raises(ValueError, match="at least one role"):
        require_role()


# The real key lookup, against the fake served over HTTP


@pytest.fixture
def served_keys(entra: FakeEntra) -> Iterator[str]:
    """Serves the fake's published keys on a free local port, the way Entra serves them."""
    body = json.dumps(entra.jwks()).encode()
    keys_path = f"/{TENANT_ID}/discovery/v2.0/keys"

    class Keys(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            ok = self.path == keys_path
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body if ok else b"{}")

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Keys)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_the_published_keys_are_fetched_and_used(
    entra: FakeEntra, served_keys: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    served = FakeEntra(authority=served_keys)
    # Same key, so the fake's tokens and the served keys match.
    served._key, served.key_id = entra._key, entra.key_id
    for name, value in served.env().items():
        monkeypatch.setenv(name, value)

    verifier = verifier_from_env()

    assert verifier is not None
    assert verifier.verify(served.token(ALICE, roles=[Role.FINANCE])).roles == {Role.FINANCE}
    with pytest.raises(SignInRefused):
        verifier.verify(served.sign(served.claims(ALICE), key_id="rolled-key"))


def test_without_settings_there_is_no_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGUS_ENTRA_TENANT_ID", raising=False)

    assert verifier_from_env() is None


def test_the_fake_serves_keys_and_tokens_like_entra() -> None:
    fake = FakeEntra()
    client = TestClient(create_fake_entra(fake))

    keys = client.get(f"/{TENANT_ID}/discovery/v2.0/keys").json()
    issued = client.post("/_standin/token", json={"object_id": ALICE, "roles": ["approver"]})
    config = client.get(f"/{TENANT_ID}/v2.0/.well-known/openid-configuration").json()

    assert keys["keys"][0]["kid"] == fake.key_id
    key = jwt.PyJWKSet.from_dict(keys)[fake.key_id].key
    token = issued.json()["access_token"]
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=API_CLIENT_ID)
    assert claims["groups"] == [ROLE_GROUPS[Role.APPROVER]]
    assert config["jwks_uri"] == fake.settings.jwks_url
    assert client.get("/_standin/env").json() == fake.env()
