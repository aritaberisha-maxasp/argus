"""Sign in with Microsoft Entra ID and role based access (NF-03, ADR 0007)."""

from argus.auth.roles import (
    CaseApprovers,
    NoApproversYet,
    Role,
    SignedInUser,
    may_view_case,
    roles_from_groups,
)
from argus.auth.tokens import (
    EntraSettings,
    SignInNotSetUp,
    SignInRefused,
    SignInUnavailable,
    TokenVerifier,
    verifier_from_env,
)
from argus.auth.web import CurrentUser, current_user, install_sign_in, require_role

__all__ = [
    "CaseApprovers",
    "CurrentUser",
    "EntraSettings",
    "NoApproversYet",
    "Role",
    "SignInNotSetUp",
    "SignInRefused",
    "SignInUnavailable",
    "SignedInUser",
    "TokenVerifier",
    "current_user",
    "install_sign_in",
    "may_view_case",
    "require_role",
    "roles_from_groups",
    "verifier_from_env",
]
