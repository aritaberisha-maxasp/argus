"""Sign in for the ARGUS web app: FastAPI dependencies (NF-03, ADR 0007).

Every route with data depends on ``current_user``. It answers
- 503 when sign in is not set up, so a missing setting never opens the app,
- 401 without a valid token,
- 403 when the person has no ARGUS role.
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from argus.auth.roles import Role, SignedInUser
from argus.auth.tokens import SignInRefused, TokenVerifier

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def install_sign_in(app: FastAPI, verifier: TokenVerifier | None) -> None:
    app.state.token_verifier = verifier


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> SignedInUser:
    verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "sign in is not set up")
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in needed", headers=_CHALLENGE)
    try:
        user = verifier.verify(credentials.credentials)
    except SignInRefused as refusal:
        # The reason only, never the token.
        log.info("sign in refused: %s", refusal.reason)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "sign in needed",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    if not user.roles:
        log.info("signed in user %s has no ARGUS role", user.object_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no ARGUS role")
    return user


CurrentUser = Annotated[SignedInUser, Depends(current_user)]


def require_role(*roles: Role) -> Callable[[SignedInUser], SignedInUser]:
    """A dependency for routes only some roles may use."""
    allowed = frozenset(roles)
    if not allowed:
        raise ValueError("name at least one role")

    def dependency(user: CurrentUser) -> SignedInUser:
        if not user.roles & allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not do this")
        return user

    return dependency
