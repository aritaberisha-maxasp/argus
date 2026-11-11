"""Who may see what in ARGUS (NF-03, ADR 0008).

Three roles come from Entra groups: approvers decide on the invoices of their vendors,
finance (Benjamin) sees and approves everything, admin (Arita) runs ARGUS. A person in none of
the three groups gets no data at all.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy import Connection

from argus.cases.model import Actor


class Role(StrEnum):
    APPROVER = "approver"
    FINANCE = "finance"
    ADMIN = "admin"


# Roles that see every case. An approver sees only the cases they decide on (AP-02, AP-06).
SEES_EVERY_CASE = frozenset({Role.FINANCE, Role.ADMIN})


@dataclass(frozen=True)
class SignedInUser:
    """A person signed in with their Microsoft account.

    ``object_id`` is their Entra object ID. It never changes, unlike a name or an email address,
    so it is what the audit log records (``Actor.user``)."""

    object_id: str
    name: str
    roles: frozenset[Role]

    @property
    def actor(self) -> Actor:
        return Actor.user(self.object_id)


def roles_from_groups(groups: Iterable[str], role_groups: Mapping[Role, str]) -> frozenset[Role]:
    """The ARGUS roles for a person in these Entra groups (object IDs)."""
    member_of = set(groups)
    return frozenset(role for role, group in role_groups.items() if group in member_of)


def may_view_case(user: SignedInUser, approvers: frozenset[str]) -> bool:
    """Finance and admin see every case. An approver sees a case only when their object ID is
    in its approver set: the vendor's approver, or the deputy while they stand in (AP-06)."""
    if user.roles & SEES_EVERY_CASE:
        return True
    return Role.APPROVER in user.roles and user.object_id in approvers


class CaseApprovers(Protocol):
    """Who decides on a case: the Entra object IDs of its approver and, if one stands in, the
    deputy. The approver table (#34) and approval (#38) provide the real one."""

    def approvers_of(self, connection: Connection, case_id: int) -> frozenset[str]: ...


class NoApproversYet:
    """Until #34 and #38 store who decides on a case, no case has an approver. So a person with
    only the approver role sees no case at all, which is the safe side."""

    def approvers_of(self, connection: Connection, case_id: int) -> frozenset[str]:
        return frozenset()
