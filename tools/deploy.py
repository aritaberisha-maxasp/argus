"""Helpers for the deploy pipeline (.github/workflows/deploy.yml, issue #81).

Three steps of the pipeline live here, so they can be tested without Azure:

    python tools/deploy.py check-config --mode deploy
        Are the GitHub variables for Azure set? None set means Azure is not set up yet: the
        pipeline skips the deploy with a notice. Some set, or a value that is not a real ID,
        fails the run with a list of what is missing.

    python tools/deploy.py app-db-role --identity-name id-argus-app --object-id <guid>
        Maps the app's managed identity to its own Entra role in PostgreSQL and makes it a
        member of argus_app, so the app signs in with its identity and works as argus_app.
        Uses ARGUS_DATABASE_URL, PGUSER and PGPASSWORD like the migrations do.

    python tools/deploy.py smoke --url https://<app> --build <commit>
        Waits until the new version answers, then checks it reaches the database as
        argus_app and the database is on the newest migration. Fails the run otherwise.

Nothing here can touch Key Vault secrets, Wise or the retention lock (NF-04, #65).
"""

import argparse
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import Connection, Engine, text

from argus.db import engine_from_env

GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

COMMON = (
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_ID",
    "ARGUS_RESOURCE_GROUP",
    "ARGUS_DATABASE_ADMIN_OBJECT_ID",
    "ARGUS_DATABASE_ADMIN_NAME",
)
# The deploy and the preview sign in as two different identities (docs/operations/deploy.md).
REQUIRED = {
    "deploy": ("AZURE_CLIENT_ID", *COMMON),
    "preview": ("AZURE_PREVIEW_CLIENT_ID", *COMMON),
}
IDS = frozenset(
    {
        "AZURE_CLIENT_ID",
        "AZURE_PREVIEW_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        "ARGUS_DATABASE_ADMIN_OBJECT_ID",
    }
)
APP_ROLE = "argus_app"


@dataclass(frozen=True)
class Config:
    configured: bool
    problems: list[str]


def check_config(mode: str, environ: Mapping[str, str]) -> Config:
    names = REQUIRED[mode]
    values = {name: environ.get(name, "").strip() for name in names}
    if not any(values.values()):
        return Config(configured=False, problems=[])
    problems = [f"GitHub variable {name} is not set" for name, value in values.items() if not value]
    for name, value in values.items():
        if value and name in IDS and (not GUID.match(value) or value == ZERO_GUID):
            problems.append(f"GitHub variable {name} is not a real Azure ID")
    return Config(configured=not problems, problems=problems)


def quote(connection: Connection, name: str) -> str:
    return connection.dialect.identifier_preparer.quote_identifier(name)


def ensure_app_role(
    principal_engine: Engine,
    app_engine: Engine,
    identity_name: str,
    object_id: str,
) -> bool:
    """Map the managed identity to an Entra role and grant it argus_app. Safe to run on every
    deploy. Returns True when the Entra role was created on this run.

    ``principal_engine`` reaches the database that holds the ``pgaadauth`` functions
    (``postgres`` on Azure). ``app_engine`` reaches the ARGUS database. The role is committed
    before the grant, because the grant runs on the other connection.
    """
    if not GUID.match(object_id) or object_id == ZERO_GUID:
        raise ValueError("the object ID of the app identity is not a real Azure ID")
    with principal_engine.begin() as connection:
        exists = connection.execute(
            text("select 1 from pg_roles where rolname = :name"), {"name": identity_name}
        ).first()
        if exists is None:
            # 'service' is the type for managed identities; not an admin, no MFA claim.
            connection.execute(
                text(
                    "select * from pgaadauth_create_principal_with_oid"
                    "(:name, :oid, 'service', false, false)"
                ),
                {"name": identity_name, "oid": object_id},
            )
    with app_engine.begin() as connection:
        login = quote(connection, identity_name)
        connection.execute(text(f"grant {APP_ROLE} to {login}"))
        # The health check compares the database's migration with the app's.
        connection.execute(text(f"grant select on alembic_version to {APP_ROLE}"))
    return exists is None


@dataclass(frozen=True)
class Check:
    ok: bool
    message: str


def check_once(client: httpx.Client, build: str, role: str) -> Check:
    try:
        health = client.get("/health")
    except httpx.HTTPError as error:
        return Check(False, f"/health not reachable yet ({type(error).__name__})")
    if health.status_code != 200:
        return Check(False, f"/health answered {health.status_code}")
    running = health.json().get("build")
    if running != build:
        return Check(False, f"/health still shows build {running}, waiting for {build}")
    try:
        database = client.get("/health/database")
    except httpx.HTTPError as error:
        return Check(False, f"/health/database not reachable ({type(error).__name__})")
    body = database.json()
    if database.status_code != 200 or body.get("status") != "ok":
        return Check(False, f"/health/database answered {database.status_code}: {body}")
    if body.get("role") != role:
        return Check(False, f"the app works as {body.get('role')}, not as {role}")
    return Check(True, f"build {build} is live, database at {body.get('revision')}, as {role}")


def smoke(
    client: httpx.Client,
    build: str,
    *,
    role: str = APP_ROLE,
    timeout: float = 600,
    interval: float = 10,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> bool:
    deadline = clock() + timeout
    while True:
        result = check_once(client, build, role)
        say(result.message)
        if result.ok:
            return True
        if clock() + interval > deadline:
            return False
        sleep(interval)


def write_output(name: str, value: str, environ: Mapping[str, str]) -> None:
    target = environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("check-config")
    config.add_argument("--mode", choices=sorted(REQUIRED), required=True)

    role = commands.add_parser("app-db-role")
    role.add_argument("--identity-name", required=True)
    role.add_argument("--object-id", required=True)
    role.add_argument(
        "--principal-database",
        default="postgres",
        help="database that holds the pgaadauth functions (postgres on Azure)",
    )

    check = commands.add_parser("smoke")
    check.add_argument("--url", required=True)
    check.add_argument("--build", required=True)
    check.add_argument("--timeout", type=float, default=600)
    check.add_argument("--interval", type=float, default=10)

    args = parser.parse_args(argv)

    if args.command == "check-config":
        result = check_config(args.mode, os.environ)
        write_output("configured", "true" if result.configured else "false", os.environ)
        if result.problems:
            for problem in result.problems:
                print(f"::error::{problem}")
            return 1
        if not result.configured:
            print(
                "::notice::Azure is not set up for ARGUS yet, so nothing was deployed. "
                "docs/operations/deploy.md lists the one time steps."
            )
        return 0

    if args.command == "app-db-role":
        app_engine = engine_from_env()
        principal_engine = engine_from_env(
            app_engine.url.set(database=args.principal_database).render_as_string(
                hide_password=False
            )
        )
        created = ensure_app_role(principal_engine, app_engine, args.identity_name, args.object_id)
        print(
            f"{'created' if created else 'kept'} database role {args.identity_name}, "
            f"member of {APP_ROLE}"
        )
        return 0

    with httpx.Client(base_url=args.url, timeout=15) as client:
        passed = smoke(client, args.build, timeout=args.timeout, interval=args.interval)
    if not passed:
        print(f"::error::smoke test failed: build {args.build} is not healthy on {args.url}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
