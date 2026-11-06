"""Helpers for the deploy pipeline (.github/workflows/deploy.yml, issue #81).

The steps of the pipeline that need logic live here, so they can be tested without Azure:

    python tools/deploy.py check-config --mode deploy [--commit <sha> --main-tip <sha>]
        Are the GitHub variables for Azure set? None set means Azure is not set up yet: the
        pipeline skips the deploy with a notice. Some set, or a value that is not a real ID,
        fails the run with a list of what is missing. With --commit and --main-tip, a commit
        that is no longer the tip of main is skipped, so a re-run of an old CI run can never
        put old code or old infrastructure live.

    python tools/deploy.py migrate
        Runs ``alembic upgrade head``. On a failure it prints only the SQLSTATE, the table,
        column and constraint names and the last good migration. The database's own error
        text is held back, because PostgreSQL puts the failing row in it and the GitHub log
        is stored outside the EU (NF-03).

    python tools/deploy.py app-db-role --identity-name id-argus-app --object-id <guid>
        Maps the app's managed identity to its own Entra role in PostgreSQL and makes it a
        member of argus_app, so the app signs in with its identity and works as argus_app.
        Uses ARGUS_DATABASE_URL, PGUSER and PGPASSWORD like the migrations do, and holds
        back database error text the same way.

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
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

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
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
HELD_BACK = (
    "The database error text is held back because it can contain row data (NF-03). "
    "Rerun the step against a copy of the database inside Azure to read it."
)


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


def latest_problem(commit: str, main_tip: str) -> str | None:
    """None when ``commit`` is the tip of main, else why it must not deploy."""
    if not main_tip:
        return "could not read the tip of main"
    if commit != main_tip:
        return f"commit {commit} is no longer the tip of main ({main_tip})"
    return None


def database_error_summary(error: BaseException) -> str:
    """What may go into a log about a database error: codes and names, never values."""
    original = getattr(error, "orig", error)
    parts = [f"SQLSTATE {getattr(original, 'sqlstate', None) or 'unknown'}"]
    diag = getattr(original, "diag", None)
    for label, attribute in (
        ("schema", "schema_name"),
        ("table", "table_name"),
        ("column", "column_name"),
        ("constraint", "constraint_name"),
    ):
        value = getattr(diag, attribute, None) if diag is not None else None
        if value:
            parts.append(f"{label} {value}")
    return ", ".join(parts)


def failure_summary(error: BaseException) -> str:
    if isinstance(error, DBAPIError):
        return database_error_summary(error)
    return type(error).__name__


def current_revision() -> str:
    try:
        with engine_from_env().connect() as connection:
            revision = connection.execute(
                text("select version_num from alembic_version")
            ).scalar_one_or_none()
    except Exception:
        return "unknown"
    return str(revision) if revision else "none"


def upgrade_to_head() -> None:
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")


def migrate(
    run_upgrade: Callable[[], None] = upgrade_to_head,
    read_revision: Callable[[], str] = current_revision,
    say: Callable[[str], None] = print,
) -> int:
    try:
        run_upgrade()
    except Exception as error:
        say(
            f"::error::The migration after revision {read_revision()} failed: "
            f"{failure_summary(error)}. {HELD_BACK}"
        )
        return 1
    say(f"The database is on revision {read_revision()}.")
    return 0


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


def json_object(response: httpx.Response) -> dict[str, Any] | None:
    """The body as a JSON object, or None. While a new revision takes over, Azure's ingress
    can answer with a plain text or HTML page, which is one more try, not a crash (QA on PR
    110)."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def check_once(client: httpx.Client, build: str, role: str) -> Check:
    try:
        health = client.get("/health")
    except httpx.HTTPError as error:
        return Check(False, f"/health not reachable yet ({type(error).__name__})")
    if health.status_code != 200:
        return Check(False, f"/health answered {health.status_code}")
    body = json_object(health)
    if body is None:
        return Check(False, f"/health answered {health.status_code}, not JSON")
    running = body.get("build")
    if running != build:
        return Check(False, f"/health still shows build {running}, waiting for {build}")
    try:
        database = client.get("/health/database")
    except httpx.HTTPError as error:
        return Check(False, f"/health/database not reachable ({type(error).__name__})")
    body = json_object(database)
    if body is None:
        return Check(False, f"/health/database answered {database.status_code}, not JSON")
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


def run_check_config(
    mode: str, commit: str | None, main_tip: str | None, environ: Mapping[str, str]
) -> int:
    result = check_config(mode, environ)
    for problem in result.problems:
        print(f"::error::{problem}")
    ready = result.configured
    failed = bool(result.problems)
    if not ready and not failed:
        print(
            "::notice::Azure is not set up for ARGUS yet, so nothing was deployed. "
            "docs/operations/deploy.md lists the one time steps."
        )
    if ready and commit is not None:
        stale = latest_problem(commit, main_tip or "")
        if stale is not None:
            ready = False
            if main_tip:
                print(f"::notice::Nothing deployed: {stale}. The newer commit deploys itself.")
            else:
                failed = True
                print(f"::error::{stale}")
    write_output("configured", "true" if ready else "false", environ)
    return 1 if failed else 0


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
    config.add_argument("--commit", help="the commit this run would deploy")
    config.add_argument("--main-tip", help="the commit main points to right now")

    commands.add_parser("migrate")

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
        return run_check_config(args.mode, args.commit, args.main_tip, os.environ)

    if args.command == "migrate":
        return migrate()

    if args.command == "app-db-role":
        app_engine = engine_from_env()
        principal_engine = engine_from_env(
            app_engine.url.set(database=args.principal_database).render_as_string(
                hide_password=False
            )
        )
        try:
            created = ensure_app_role(
                principal_engine, app_engine, args.identity_name, args.object_id
            )
        except Exception as error:
            print(f"::error::Giving the app its database role failed: {failure_summary(error)}.")
            print(HELD_BACK)
            return 1
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
