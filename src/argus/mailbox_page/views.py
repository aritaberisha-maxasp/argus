"""What the demo mailbox page shows: every folder of the fake mailbox and the emails in it.

Read through the same Graph client as the sweep, against the fake Graph only (#135).
"""

from dataclasses import dataclass, field
from datetime import datetime

from argus.intake.graph import FolderMessage, GraphClient, MailFolder

# The fake mailbox has the Inbox, Deleted Items and ARGUS with its five subfolders. These limits
# only keep a broken stand in from making the page walk forever.
MAX_DEPTH = 3
MAX_FOLDERS = 50


@dataclass(frozen=True)
class FolderView:
    path: str  # for example "ARGUS/Incoming invoices"
    emails: list[FolderMessage] = field(default_factory=list)
    more: bool = False  # more emails than the page shows


def _order(path: str) -> tuple[int, str]:
    """Inbox first, then ARGUS and its subfolders, then the rest by name."""
    if path == "Inbox":
        return (0, "")
    if path == "ARGUS" or path.startswith("ARGUS/"):
        return (1, path.lower())
    return (2, path.lower())


def mailbox_folders(graph: GraphClient) -> list[FolderView]:
    """Every folder of the mailbox with its newest emails, Inbox first."""
    found: list[tuple[str, MailFolder]] = []

    def walk(parent: MailFolder | None, prefix: str, depth: int) -> None:
        for folder in graph.folders(parent.id if parent else None):
            if len(found) >= MAX_FOLDERS:
                return
            path = f"{prefix}{folder.name}"
            found.append((path, folder))
            if depth < MAX_DEPTH:
                walk(folder, f"{path}/", depth + 1)

    walk(None, "", 1)
    views = []
    for path, folder in sorted(found, key=lambda pair: _order(pair[0])):
        emails, more = graph.folder_messages(folder.id)
        views.append(FolderView(path=path, emails=emails, more=more))
    return views


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def folders_json(mailbox: str, folders: list[FolderView]) -> dict[str, object]:
    return {
        "mailbox": mailbox,
        "folders": [
            {
                "path": f.path,
                "more": f.more,
                "emails": [
                    {
                        "subject": e.subject,
                        "sender_name": e.sender_name,
                        "sender_address": e.sender_address,
                        "received_at": _iso(e.received_at),
                    }
                    for e in f.emails
                ],
            }
            for f in folders
        ],
    }
