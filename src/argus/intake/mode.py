"""Shadow mode or live mode: whether ARGUS may act on the mailbox (#72, migration 0012).

In shadow mode, the default, ARGUS stores every email and proposes a route for it, but it does
not classify, route, move, forward or delete anything. A person still sorts the Inbox. In live
mode ARGUS does all of that itself.

Only a person can switch, never a job: the database refuses the change without a ``user:``
actor and writes ``intake_mode_changed`` into the audit log, with the person's name and their
reason. The web app lets only the admin role (Arita) do it.

Every step that acts on the mailbox asks :func:`is_live` right before it calls Graph, not only
at the start of a sweep, so switching back to shadow mode stops ARGUS within one email.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work

# Spaces of any kind at either end: Python's own whitespace (tabs, line breaks, non-breaking
# spaces) plus the zero width ones it does not count. The database trims the same way with
# ``correction_trim`` (migrations 0013 and 0017), so tabs or line breaks alone are no reason.
_EDGE_SPACES = re.compile(r"^[\s\u180e\u200b\ufeff]+|[\s\u180e\u200b\ufeff]+$")
REASON_MIN, REASON_MAX = 3, 500


def trimmed_reason(reason: str) -> str:
    """The reason without spaces, tabs or line breaks at either end (#170)."""
    return _EDGE_SPACES.sub("", reason)


class StoppedByShadowMode(RuntimeError):
    """ARGUS was switched to shadow mode while it was about to act on the mailbox."""


class IntakeMode(StrEnum):
    SHADOW = "shadow"  # propose only, a person sorts the Inbox
    LIVE = "live"  # ARGUS sorts, routes, files and removes on its own


def intake_mode(connection: Connection) -> IntakeMode:
    """The mode as the database holds it. Without a row it is shadow, the safe side."""
    found: str = connection.execute(text("select intake_mode_now()")).scalar_one()
    return IntakeMode(found)


def is_live(engine: Engine) -> bool:
    """Whether ARGUS may act on the mailbox right now."""
    with engine.connect() as connection:
        return intake_mode(connection) is IntakeMode.LIVE


@dataclass(frozen=True)
class SwitchedBy:
    """Who switched and why, as they were at that moment (ADR 0008): the object ID alone names
    nobody once the Entra account is gone."""

    actor: Actor  # user:<Entra object ID>
    name: str
    reason: str
    sign_in_address: str | None = None


def switch_intake_mode(engine: Engine, by: SwitchedBy, mode: IntakeMode) -> bool:
    """Switch to ``mode``. Returns False when ARGUS already ran in that mode, in which case
    nothing changes and nothing is logged. The database refuses a job as actor, and a switch
    without a name or a reason, and writes all of it into ``intake_mode_changed``."""
    if by.actor.kind != "user":
        raise PermissionError("only a person can switch between shadow and live mode")
    with unit_of_work(engine, by.actor) as db:
        for setting, value in (
            ("argus.switch_name", by.name),
            ("argus.switch_address", by.sign_in_address or ""),
            ("argus.switch_reason", by.reason),
        ):
            db.execute(
                text("select set_config(:setting, :value, true)"),
                {"setting": setting, "value": value},
            )
        changed = db.execute(
            text("update intake_mode set mode = :mode where mode <> :mode returning mode"),
            {"mode": mode.value},
        ).first()
    return changed is not None
