"""Routes to keep the approver table (#34, AP-01, AP-06, PY-13, VM-02).

Only the admin role (Arita) changes the table. Finance (Benjamin) and admin may read it. An
approver gets 403, someone without a token 401. There is no page yet; these routes are what a
page will call.

The database writes every change into the audit log with the admin's object ID and name. The app
log names only the vendor account and counts, never a person (NF-03).
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine

from argus.approvers.table import (
    OBJECT_ID,
    VENDOR_ACCOUNT,
    ChangedBy,
    list_approvers,
    list_vendor_approvers,
    remove_approver,
    remove_vendor_approver,
    save_approver,
    set_absent,
    set_vendor_approver,
)
from argus.auth import Role, SignedInUser, require_role
from argus.cases.store import CaseRuleBroken

log = logging.getLogger(__name__)

Admin = Annotated[SignedInUser, Depends(require_role(Role.ADMIN))]
FinanceOrAdmin = Annotated[SignedInUser, Depends(require_role(Role.FINANCE, Role.ADMIN))]
# Taken in either case, as pasted from the Azure portal, and lowercased before use.
ObjectId = Annotated[
    str,
    Path(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
]
VendorAccount = Annotated[str, Path(pattern=VENDOR_ACCOUNT.pattern)]


def _object_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if not OBJECT_ID.match(value):
        raise ValueError("must be an Entra object ID")
    return value


class ApproverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    # The same simple shape the database checks. Graph tells whether the address really exists.
    email: str = Field(max_length=320, pattern=r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$")


class AbsenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    absent: bool


class VendorApproverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approver: str
    deputy: str | None = None
    direct_debit: bool = False

    @field_validator("approver", "deputy")
    @classmethod
    def entra_object_id(cls, value: str | None) -> str | None:
        return _object_id(value)


class ApproverOut(BaseModel):
    object_id: str
    name: str
    email: str
    absent: bool


class VendorApproverOut(BaseModel):
    vendor_account: str
    approver: str
    deputy: str | None
    direct_debit: bool


class ApproverTableOut(BaseModel):
    approvers: list[ApproverOut]
    vendors: list[VendorApproverOut]


class Changed(BaseModel):
    changed: bool  # False when it already was so; nothing was logged then


def _by(user: SignedInUser) -> ChangedBy:
    return ChangedBy(actor=user.actor, name=user.name, sign_in_address=user.sign_in_address)


def _refused(error: CaseRuleBroken) -> HTTPException:
    # The database's own reason names no person and no value, so it can go back to the admin.
    return HTTPException(status.HTTP_409_CONFLICT, str(error))


def install_approver_routes(app: FastAPI, engine: Callable[[], Engine]) -> None:
    @app.get("/approvers")
    def read_approver_table(user: FinanceOrAdmin) -> ApproverTableOut:
        """Every person who can approve, and every vendor's approver, deputy and direct debit."""
        with engine().connect() as connection:
            people = list_approvers(connection)
            vendors = list_vendor_approvers(connection)
        return ApproverTableOut(
            approvers=[
                ApproverOut(object_id=p.object_id, name=p.name, email=p.email, absent=p.absent)
                for p in people
            ],
            vendors=[
                VendorApproverOut(
                    vendor_account=v.vendor_account,
                    approver=v.approver_id,
                    deputy=v.deputy_id,
                    direct_debit=v.direct_debit,
                )
                for v in vendors
            ],
        )

    @app.put("/approvers/{object_id}")
    def put_approver(object_id: ObjectId, body: ApproverIn, user: Admin) -> Changed:
        """Add a person who can approve, by Entra object ID, or change their name or address."""
        try:
            changed = save_approver(
                engine(), _by(user), object_id.lower(), body.name.strip(), body.email.strip()
            )
        except CaseRuleBroken as error:
            raise _refused(error) from None
        return Changed(changed=changed)

    @app.delete("/approvers/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_approver(object_id: ObjectId, user: Admin) -> Response:
        """Remove a person. Refused (409) while they approve or stand in for a vendor."""
        try:
            removed = remove_approver(engine(), _by(user), object_id.lower())
        except CaseRuleBroken as error:
            raise _refused(error) from None
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approver")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.put("/approvers/{object_id}/absence")
    def put_absence(object_id: ObjectId, body: AbsenceIn, user: Admin) -> Changed:
        """Mark a person away, so their deputies decide in their place (AP-06), or back."""
        try:
            changed = set_absent(engine(), _by(user), object_id.lower(), body.absent)
        except CaseRuleBroken as error:
            raise _refused(error) from None
        if changed is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approver")
        return Changed(changed=changed)

    @app.put("/vendors/{vendor_account}/approver")
    def put_vendor_approver(
        vendor_account: VendorAccount, body: VendorApproverIn, user: Admin
    ) -> Changed:
        """Give a vendor account its one approver (AP-01), a deputy (AP-06) and the direct debit
        flag (PY-13). Both people must be in the table first."""
        try:
            changed = set_vendor_approver(
                engine(), _by(user), vendor_account, body.approver, body.deputy, body.direct_debit
            )
        except CaseRuleBroken as error:
            raise _refused(error) from None
        if changed:
            log.info("approver row of vendor %s set", vendor_account)
        return Changed(changed=changed)

    @app.delete("/vendors/{vendor_account}/approver", status_code=status.HTTP_204_NO_CONTENT)
    def delete_vendor_approver(vendor_account: VendorAccount, user: Admin) -> Response:
        """Take a vendor out of the table. Its next invoices go to the exception queue (VM-02)."""
        try:
            removed = remove_vendor_approver(engine(), _by(user), vendor_account)
        except CaseRuleBroken as error:
            raise _refused(error) from None
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "this vendor has no approver")
        log.info("approver row of vendor %s removed", vendor_account)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
