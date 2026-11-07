"""Shadow mode or live mode: whether ARGUS may act on the mailbox (#72, migration 0012).

In shadow mode, the default, ARGUS stores every email and proposes a route for it, but it does
not classify, route, move, forward or delete anything. A person still sorts the Inbox. In live
mode ARGUS does all of that itself.

Only a person can switch, never a job: the database refuses the change without a ``user:``
actor and writes ``intake_mode_changed`` into the audit log. The web app lets only the admin
role (Arita) do it.

Every step that acts on the mailbox asks :func:`is_live` right before it calls Graph, not only
at the start of a sweep, so switching back to shadow mode stops ARGUS within one email.
"""

from enum import StrEnum

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work


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


def switch_intake_mode(engine: Engine, actor: Actor, mode: IntakeMode) -> bool:
    """Switch to ``mode`` as ``actor``. Returns False when ARGUS already ran in that mode, in
    which case nothing changes and nothing is logged. The database refuses a job as actor."""
    if actor.kind != "user":
        raise PermissionError("only a person can switch between shadow and live mode")
    with unit_of_work(engine, actor) as db:
        changed = db.execute(
            text("update intake_mode set mode = :mode where mode <> :mode returning mode"),
            {"mode": mode.value},
        ).first()
    return changed is not None
