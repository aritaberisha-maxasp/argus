"""The only Wise calls ARGUS may make (ADR 0004, NF-04).

ARGUS prepares payments in Wise but must never fund or release them. Every request
the Wise client sends is checked here first, and anything that is not on the list is
refused before it leaves ARGUS. The list has no call that funds a transfer or a batch
group, no call that edits a recipient and no webhook subscription. Adding a call to
this list needs a compliance review.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

# Wise uses numbers for profile, recipient and transfer IDs and UUIDs for batch groups.
_NUMBER = "[0-9]+"
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PLACEHOLDERS = {
    "accountId": _NUMBER,
    "batchGroupId": _UUID,
    "profileId": _NUMBER,
    "transferId": _NUMBER,
}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class WiseCallRefused(PermissionError):
    """ARGUS refused to send a Wise request because it is not on the allowlist."""


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse a key given twice, so the guard and Wise cannot read different values."""
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("repeated key in JSON body")
    return dict(pairs)


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        data = json.loads(body, object_pairs_hook=_no_repeated_keys)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _only_complete(body: bytes, profile_id: int) -> bool:
    """A batch group may only be marked COMPLETED, which hands it over unfunded."""
    data = _json_object(body)
    return (
        data is not None
        and data.get("status") == "COMPLETED"
        and set(data) <= {"status", "version"}
    )


def _own_profile(body: bytes, profile_id: int) -> bool:
    """A recipient names its profile in the body, so the profile check happens here."""
    data = _json_object(body)
    profile = None if data is None else data.get("profile")
    return type(profile) is int and profile == profile_id


@dataclass(frozen=True)
class AllowedCall:
    method: str
    template: str
    purpose: str
    body_check: Callable[[bytes, int], bool] | None = None

    def pattern(self) -> re.Pattern[str]:
        parts = _PLACEHOLDER.split(self.template)
        regex = "".join(
            f"(?P<{part}>{_PLACEHOLDERS[part]})" if i % 2 else re.escape(part)
            for i, part in enumerate(parts)
        )
        return re.compile(regex)


ALLOWED_CALLS: tuple[AllowedCall, ...] = (
    AllowedCall(
        "POST",
        "/v1/accounts",
        "create a recipient from DATEV bank details",
        body_check=_own_profile,
    ),
    AllowedCall("GET", "/v1/accounts/{accountId}", "read a recipient to compare it with DATEV"),
    AllowedCall("POST", "/v3/profiles/{profileId}/quotes", "quote the exact invoice amount"),
    AllowedCall(
        "GET", "/v3/profiles/{profileId}/batch-groups", "find batch groups ARGUS did not create"
    ),
    AllowedCall("POST", "/v3/profiles/{profileId}/batch-groups", "open a batch group for a run"),
    AllowedCall(
        "GET", "/v3/profiles/{profileId}/batch-groups/{batchGroupId}", "read a batch group back"
    ),
    AllowedCall(
        "PATCH",
        "/v3/profiles/{profileId}/batch-groups/{batchGroupId}",
        "mark a batch group COMPLETED, unfunded",
        body_check=_only_complete,
    ),
    AllowedCall(
        "POST",
        "/v3/profiles/{profileId}/batch-groups/{batchGroupId}/transfers",
        "add one vendor payment to a batch group",
    ),
    AllowedCall("GET", "/v1/transfers/{transferId}", "read the status of a transfer"),
    AllowedCall("GET", "/v1/rates", "read the Wise exchange rate"),
)

_COMPILED = tuple((call, call.pattern()) for call in ALLOWED_CALLS)
# Anything outside these characters (encoded bytes, dots, a second slash) is refused
# before matching, so a path cannot be dressed up to look like an allowed one.
_SAFE_PATH = re.compile(r"(/[A-Za-z0-9_-]+)+")


def check_call(method: str, path: str, body: bytes, profile_id: int) -> AllowedCall:
    """Return the allowlist entry for this request or raise ``WiseCallRefused``.

    ``path`` is the raw request path without the query string. ``profile_id`` is the
    one Wise profile ARGUS works for; a call to any other profile is refused.
    """
    if not _SAFE_PATH.fullmatch(path):
        raise WiseCallRefused(f"refused Wise call {method} {path!r}: path is not plain")
    for call, pattern in _COMPILED:
        match = pattern.fullmatch(path)
        if call.method != method or match is None:
            continue
        profile = match.groupdict().get("profileId")
        if profile is not None and int(profile) != profile_id:
            raise WiseCallRefused(f"refused Wise call {method} {path}: not ARGUS's profile")
        if call.body_check is not None and not call.body_check(body, profile_id):
            raise WiseCallRefused(f"refused Wise call {method} {path}: body not allowed")
        return call
    raise WiseCallRefused(f"refused Wise call {method} {path}: not on the allowlist")
