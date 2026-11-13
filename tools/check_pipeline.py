"""Checks the rules of #81 on the deploy and preview workflows.

    python tools/check_pipeline.py [repository root]

The deploy pipeline cannot be run in CI without Azure, so this check reads the workflow files
and fails when one of the promises in the issue no longer holds:

- Azure sign in uses OpenID Connect only. No secret of any kind is read from GitHub.
- The deploy runs only on main, after CI passed or by hand. Never on a PR.
- Only the tip of main deploys, so a re-run of an old CI run is skipped.
- The order keeps the running version alive: infrastructure with the running image, then the
  migrations, then the new image, then the smoke test. Nothing in the deploy may carry on
  after an error, and none of those steps may have its own condition.
- The migrations run through ``tools/deploy.py migrate``, which keeps row data out of the
  log (NF-03). A firewall rule that cannot be closed fails the run.
- The preview never changes anything in Azure.
- No workflow nor its scripts touch the retention lock, the immutability policy, a
  Complete mode deployment, Key Vault secrets or Wise (#65, NF-04).
- The stand ins get their new image only with the new app version, never before the
  migrations (#124).
- The Demo workflow runs by hand on main only, refuses unless the running app is in demo mode,
  resets only through ``python -m argus.demo reset``, which checks every target is demo data,
  and never deploys or pushes anything (#124, ADR 0009).
"""

import ast
import re
import sys
from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(".github/workflows/deploy.yml")
PREVIEW = Path(".github/workflows/infra-preview.yml")
DEMO = Path(".github/workflows/demo.yml")
SCRIPTS = (Path("tools/azure"), Path("tools/deploy.py"))
LOGIN_ACTION = "azure/login@"
LOGIN_INPUTS = frozenset({"client-id", "tenant-id", "subscription-id"})
SECRET_EXPRESSION = re.compile(r"\$\{\{[^}]*\bsecrets\.")
DEPLOY_ORDER = ("infra", "image", "migrate", "release", "smoke")

# Nothing in the pipeline may change the lock, read a vault secret or talk to Wise.
FORBIDDEN_ANYWHERE = {
    "the retention lock": re.compile(r"retention_?locked", re.IGNORECASE),
    # Changing the immutability policy directly, or a Complete deployment that removes what
    # the templates do not list, would get around the lock as well (compliance on PR 110).
    "the locked document store": re.compile(
        r"immutability|legal-hold|--mode\s+complete", re.IGNORECASE
    ),
    "Key Vault secrets": re.compile(r"az\s+keyvault|vaults/secrets|\.vault\.azure\.net"),
    "Wise": re.compile(r"\bwise\b|api\.(sandbox\.)?transferwise|wise\.com", re.IGNORECASE),
}
# The preview may look, never change.
FORBIDDEN_IN_PREVIEW = {
    "a deployment": re.compile(r"deployment\s+(group|sub)\s+create"),
    "an image push": re.compile(r"docker\s+push|az\s+acr"),
    "the database": re.compile(r"alembic|firewall-rule|oss-rdbms"),
    "a resource change": re.compile(r"az\s+\S+\s+(update|create|delete|set)\b"),
}


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a workflow")
    # YAML 1.1 reads the key "on" as True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    found = job.get("steps", [])
    return found if isinstance(found, list) else []


def runs(workflow: dict[str, Any]) -> str:
    return "\n".join(
        str(step.get("run", "")) for job in workflow.get("jobs", {}).values() for step in steps(job)
    )


def sign_in_problems(name: str, workflow: dict[str, Any], text: str) -> list[str]:
    problems = []
    if SECRET_EXPRESSION.search(text):
        problems.append(f"{name} reads a GitHub secret; Azure sign in must use OpenID Connect")
    if workflow.get("permissions") != {"contents": "read"}:
        problems.append(f"{name} must default to 'contents: read' permissions")
    for job_name, job in workflow.get("jobs", {}).items():
        logins = [s for s in steps(job) if str(s.get("uses", "")).startswith(LOGIN_ACTION)]
        wants_token = job.get("permissions", {}).get("id-token") == "write"
        if wants_token and not logins:
            problems.append(f"{name} job {job_name} asks for a sign in token it does not use")
        for login in logins:
            if not wants_token:
                problems.append(f"{name} job {job_name} signs in without OpenID Connect")
            given = set(login.get("with", {}))
            if given != LOGIN_INPUTS:
                extra = ", ".join(sorted(given - LOGIN_INPUTS)) or "none"
                problems.append(
                    f"{name} job {job_name} signs in with other inputs than the three IDs "
                    f"(extra: {extra})"
                )
    return problems


def deploy_problems(workflow: dict[str, Any]) -> list[str]:
    problems = []
    triggers = workflow.get("on", {})
    if not isinstance(triggers, dict) or set(triggers) != {"workflow_run", "workflow_dispatch"}:
        problems.append("deploy.yml may only run after CI or by hand")
    else:
        after = triggers["workflow_run"] or {}
        if after.get("workflows") != ["CI"] or after.get("branches") != ["main"]:
            problems.append("deploy.yml must run after CI on main only")
    jobs = workflow.get("jobs", {})
    gate = jobs.get("config", {})
    condition = str(gate.get("if", ""))
    if "github.ref == 'refs/heads/main'" not in condition or "conclusion == 'success'" not in (
        condition
    ):
        problems.append("deploy.yml config job must require main and a passed CI run")
    check_runs = " ".join(str(step.get("run", "")) for step in steps(gate))
    if "check-config" not in check_runs or "--main-tip" not in check_runs:
        problems.append("deploy.yml config job must skip commits that are not the tip of main")
    deploy = jobs.get("deploy", {})
    if deploy.get("needs") != "config":
        problems.append("deploy.yml deploy job must wait for the config job")
    order = [str(step.get("id")) for step in steps(deploy) if step.get("id") in DEPLOY_ORDER]
    if order != list(DEPLOY_ORDER):
        problems.append(f"deploy.yml steps must run in the order {', '.join(DEPLOY_ORDER)}")
    by_id = {step.get("id"): step for step in steps(deploy)}
    infra_image = str(by_id.get("infra", {}).get("env", {}).get("ARGUS_APP_IMAGE", ""))
    if "steps.current." not in infra_image:
        problems.append("deploy.yml must apply the infrastructure with the running image")
    release_image = str(by_id.get("release", {}).get("env", {}).get("ARGUS_APP_IMAGE", ""))
    if "steps.image." not in release_image:
        problems.append("deploy.yml must deploy the image it just pushed")
    if "ARGUS_DEMO_IMAGE" in by_id.get("infra", {}).get("env", {}):
        problems.append("deploy.yml must not hand the stand ins a new image before the migrations")
    demo_image = str(by_id.get("release", {}).get("env", {}).get("ARGUS_DEMO_IMAGE", ""))
    if "steps.image." not in demo_image:
        problems.append("deploy.yml must deploy the stand ins from the demo image it just pushed")
    if "tools/deploy.py migrate" not in str(by_id.get("migrate", {}).get("run", "")):
        problems.append("deploy.yml migrate step must run tools/deploy.py migrate")
    if "tools/deploy.py smoke" not in str(by_id.get("smoke", {}).get("run", "")):
        problems.append("deploy.yml smoke step must run tools/deploy.py smoke")
    if re.search(r"\balembic\b", runs(workflow)):
        problems.append(
            "deploy.yml must run the migrations through tools/deploy.py, which keeps row "
            "data out of the log (NF-03)"
        )
    for step_id in DEPLOY_ORDER:
        if "if" in by_id.get(step_id, {}):
            problems.append(f"deploy.yml step {step_id} may not have its own if condition")
    if "||" in str(by_id.get("close-firewall", {}).get("run", "")):
        problems.append("deploy.yml must fail when the database firewall cannot be closed")
    for job_name, job in jobs.items():
        if job.get("continue-on-error"):
            problems.append(f"deploy.yml job {job_name} may not continue on error")
        for step in steps(job):
            if step.get("continue-on-error"):
                problems.append(f"deploy.yml step {step.get('name')} may not continue on error")
    return problems


def preview_problems(workflow: dict[str, Any], text: str) -> list[str]:
    problems = []
    triggers = workflow.get("on", {})
    if not isinstance(triggers, dict) or set(triggers) != {"pull_request"}:
        problems.append("infra-preview.yml must run on pull requests only")
    if "what-if" not in text:
        problems.append("infra-preview.yml must run a what if")
    for what, pattern in FORBIDDEN_IN_PREVIEW.items():
        if pattern.search(text):
            problems.append(f"infra-preview.yml must not make {what}")
    return problems


# The Demo workflow may restart apps, start the seed job and rebuild the demo database. It may
# not deploy, push an image, or run SQL or migrations on its own.
FORBIDDEN_IN_DEMO = {
    "a deployment": re.compile(r"deployment\s+(group|sub)\s+create|apply_infra"),
    "an image push": re.compile(r"docker\s+push|az\s+acr"),
    "its own SQL or migrations": re.compile(r"\b(alembic|psql)\b|deploy\.py\s+migrate"),
}


def demo_problems(workflow: dict[str, Any], text: str) -> list[str]:
    problems = []
    triggers = workflow.get("on", {})
    if not isinstance(triggers, dict) or set(triggers) != {"workflow_dispatch"}:
        problems.append("demo.yml may only run by hand")
    # Sharing the deploy's queue would cancel a deploy waiting in it (review of PR 130).
    group = str((workflow.get("concurrency") or {}).get("group", ""))
    if not group or "deploy" in group:
        problems.append("demo.yml needs a concurrency group of its own, not the deploy's")
    jobs = workflow.get("jobs", {})
    for job_name, job in jobs.items():
        if "github.ref == 'refs/heads/main'" not in str(job.get("if", "")):
            problems.append(f"demo.yml job {job_name} must run on main only")
        if job.get("continue-on-error"):
            problems.append(f"demo.yml job {job_name} may not continue on error")
        names = [str(step.get("run", "")) for step in steps(job)]
        check = next((i for i, run in enumerate(names) if "demo=true" in run), None)
        reset = next((i for i, run in enumerate(names) if "argus.demo reset" in run), None)
        if reset is not None and (check is None or check > reset):
            problems.append("demo.yml must check that the running app is in demo mode first")
        if reset is not None:
            given = str(steps(job)[reset].get("env", {}).get("ARGUS_DEMO", ""))
            if "steps.target.outputs.demo" not in given or "ARGUS_DEMO=" in names[reset]:
                problems.append(
                    "demo.yml must hand the reset the running app's own ARGUS_DEMO, so the "
                    "reset checks demo mode again"
                )
        for step in steps(job):
            if step.get("continue-on-error"):
                problems.append(f"demo.yml step {step.get('name')} may not continue on error")
            if "firewall-rule delete" in str(step.get("run", "")) and "||" in str(step["run"]):
                problems.append("demo.yml must fail when the database firewall cannot be closed")
            # az prints a list in tsv as one value per line, so splitting it into columns
            # handed app-db-role both values as the object ID (#148).
            run = str(step.get("run", ""))
            if "app-db-role" in run and "--query principalId" not in run:
                problems.append(
                    "demo.yml must read the app identity's object ID with --query principalId"
                )
    for what, pattern in FORBIDDEN_IN_DEMO.items():
        if pattern.search(code_only(text)):
            problems.append(f"demo.yml must not make {what}")
    return problems


def forbidden_problems(name: str, text: str) -> list[str]:
    return [
        f"{name} mentions {what}"
        for what, pattern in FORBIDDEN_ANYWHERE.items()
        if pattern.search(code_only(text))
    ]


def code_only(text: str) -> str:
    """Drop shell comments, so the rules can be explained in the files they guard."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def python_code(text: str) -> str:
    """The code of a Python file without comments and docstrings."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(tree)


def check(root: Path) -> list[str]:
    problems: list[str] = []
    deploy, preview, demo = load(root / DEPLOY), load(root / PREVIEW), load(root / DEMO)
    problems += sign_in_problems("deploy.yml", deploy, (root / DEPLOY).read_text("utf-8"))
    problems += sign_in_problems("infra-preview.yml", preview, (root / PREVIEW).read_text("utf-8"))
    problems += sign_in_problems("demo.yml", demo, (root / DEMO).read_text("utf-8"))
    problems += deploy_problems(deploy)
    problems += preview_problems(preview, runs(preview))
    problems += demo_problems(demo, runs(demo))
    problems += forbidden_problems("deploy.yml", runs(deploy))
    problems += forbidden_problems("infra-preview.yml", runs(preview))
    problems += forbidden_problems("demo.yml", runs(demo))
    for entry in SCRIPTS:
        folder = root / entry
        files = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else [folder]
        for path in files:
            text = path.read_text("utf-8")
            code = python_code(text) if path.suffix == ".py" else text
            problems += forbidden_problems(str(path.relative_to(root)), code)
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    problems = check(root)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print("The deploy, preview and demo workflows keep the rules of #81 and #124.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
