"""``python -m argus.demo seed``, ``send`` and ``reset`` (#124, #136).

seed
    Puts the demo emails into the fake mailbox at ``ARGUS_GRAPH_URL``. In Azure this runs as
    the Container Apps job ``caj-argus-demo-seed``, inside the environment, because the fake
    mailbox cannot be reached from outside.

send <email>
    Drops one chosen test email, for example ``E12``, into the Inbox of the fake mailbox, so
    the demo can show an email arriving (#136). In Azure the Demo workflow runs this inside the
    seed job, like ``seed``. The next sweep picks the email up.

reset [--standins]
    Empties the demo database (``ARGUS_DATABASE_URL``, its name must end in ``_demo``) and
    runs the migrations again and fills the approver table from the synthetic fixture (#35),
    then deletes the files in the demo document store
    (``ARGUS_DOCUMENTS_CONNECTION_STRING``, its account name must contain ``demo``). With
    ``--standins`` it also asks the stand ins to start over. In Azure the Demo workflow runs
    this as the database admin and restarts the stand ins itself.

All three refuse to run unless ``ARGUS_DEMO=true`` and the settings keep the demo rule of
:mod:`argus.connections`. Messages name settings, never values or email content.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

import httpx
from azure.storage.blob import BlobServiceClient

from argus.connections import ConnectionSettingsError, connections_from_env
from argus.db import DATABASE_URL_ENV, engine_from_env
from argus.demo.common import DemoRefused
from argus.demo.data import emails_folder
from argus.demo.reset import (
    check_demo_database,
    check_demo_documents,
    load_demo_approvers,
    reset_database,
    reset_documents,
    reset_standins,
)
from argus.demo.seed import seed, send_one
from argus.documents.store import CONNECTION_STRING_SECRET, DEFAULT_CONTAINER


def run_seed(environ: Mapping[str, str], folder: str | None) -> int:
    connections = connections_from_env(environ)
    seeded = seed(connections, emails_folder(folder))
    for email in seeded:
        print(f"seeded {email.file}: {email.shows}")
    print(f"{len(seeded)} demo emails are in the fake mailbox.")
    return 0


def run_send(environ: Mapping[str, str], folder: str | None, email_id: str) -> int:
    connections = connections_from_env(environ)
    sent = send_one(connections, emails_folder(folder), email_id)
    print(f"sent {sent.file} to the Inbox of the fake mailbox; the next sweep picks it up.")
    return 0


def run_reset(environ: Mapping[str, str], standins: bool) -> int:
    connections = connections_from_env(environ)
    if not connections.demo:
        raise DemoRefused("demo mode is off (ARGUS_DEMO), so there is no demo data to reset")
    url = environ.get(DATABASE_URL_ENV, "").strip()
    documents = environ.get(CONNECTION_STRING_SECRET, "").strip()
    if not url and not documents and not standins:
        raise DemoRefused(
            f"nothing to reset: set {DATABASE_URL_ENV} or {CONNECTION_STRING_SECRET}, "
            "or pass --standins"
        )
    # Check every target before changing any, so a wrong setting changes nothing at all.
    if url:
        check_demo_database(url)
    if documents:
        check_demo_documents(documents)
    if url:
        engine = engine_from_env(url, environ=environ)
        try:
            revision = reset_database(engine)
            approvers = load_demo_approvers(engine)
        finally:
            engine.dispose()
        print(f"demo database emptied, migrations run again up to {revision}")
        print(f"demo approver table filled, {approvers} vendors have an approver")
    if documents:
        service = BlobServiceClient.from_connection_string(documents)
        deleted = reset_documents(service.get_container_client(DEFAULT_CONTAINER))
        print(f"demo document store emptied, {deleted} files deleted")
    if standins:
        with httpx.Client(timeout=30) as http:
            for label in reset_standins(connections, http):
                print(f"{label} starts over")
    return 0


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m argus.demo", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed_command = commands.add_parser("seed", help="put the demo emails into the fake mailbox")
    seed_command.add_argument("--emails", help="folder with the fixture emails")
    send_command = commands.add_parser(
        "send", help="drop one chosen test email into the fake mailbox"
    )
    send_command.add_argument("email", help="the test email by its number, for example E12")
    send_command.add_argument("--emails", help="folder with the fixture emails")
    reset_command = commands.add_parser("reset", help="empty the demo data")
    reset_command.add_argument(
        "--standins", action="store_true", help="also ask the stand ins to start over"
    )
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        if args.command == "seed":
            return run_seed(environ, args.emails)
        if args.command == "send":
            return run_send(environ, args.emails, args.email)
        return run_reset(environ, args.standins)
    except (DemoRefused, ConnectionSettingsError) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
