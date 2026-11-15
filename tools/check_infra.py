"""Check the compiled Azure infrastructure code against the rules of issue #65.

CI compiles ``infra/main.bicep`` and ``infra/policy/eu-only.bicep`` to ARM JSON with
``az bicep build`` and then runs this script on the output folder. Nothing is deployed and no
Azure sign in is needed. The script fails when:

- a resource could end up outside Germany West Central, or an AI account outside the EU
  (ADR 0001, ADR 0006, NF-03);
- the retention lock for original documents is on by default, or switched on anywhere but
  through the ``retentionLocked`` parameter, or the retention period is shorter than 10 years
  from the end of the calendar year (ADR 0001, NF-01);
- the database keeps its own backups for less than 35 days, or no Backup vault keeps the
  monthly backups for the legal period (at least 10 years by default, set through the
  ``databaseBackupYears`` parameter), or the vault could be locked any other way than through
  ``retentionLocked``, or is not immutable (Unlocked) before that lock, or soft delete is
  off (#82, ADR 0007, NF-01);
- the Backup vault raises no alert for a failed job, no alert processing rule sends Azure
  Backup alerts to an action group, the vault's job records do not reach Log Analytics, no log
  alert catches a week without a completed backup, or that alert checks on another day than the
  one after the weekly backup (#115, NF-01);
- a secret could end up in the code, a parameter default or a deployment output, or the app
  gets a secret any other way than from Key Vault (NF-06);
- a model deployment, the allowed deployment types or the Azure Policy would let a Global
  deployment type through (ADR 0006);
- demo mode is on by default in the template, a stand in can be reached from the internet, the
  stand ins could run without demo mode, or the parameters file puts real secrets next to demo
  mode (#124, ADR 0009);
- the mailbox sweep does not run every 5 minutes or the removal not once a day as scheduled
  jobs, a job retries or runs twice at once, either job can run with no alert watching it, an
  alert has no action group, or an alert email address is written in the code (#125);
- the ARGUS web app does not always get the five sign in settings, or gets one as a fixed value
  instead of from a parameter, or ``ARGUS_ENTRA_AUTHORITY`` (the local fake only) shows up
  anywhere (#23, #133, NF-03).

Run from the repository root: ``python tools/check_infra.py build/infra``
"""

import json
import re
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

HOME_REGION = "germanywestcentral"
EU_REGIONS = frozenset(
    {
        "germanywestcentral",
        "germanynorth",
        "swedencentral",
        "francecentral",
        "westeurope",
        "northeurope",
        "polandcentral",
        "italynorth",
        "spaincentral",
    }
)
EU_DEPLOYMENT_SKUS = frozenset({"DataZoneStandard", "Standard"})
AI_LOCATION = "[parameters('aiLocation')]"
LOCATION_EXPRESSIONS = frozenset({"[parameters('location')]", AI_LOCATION})
AI_ACCOUNT = "Microsoft.CognitiveServices/accounts"
LOCK_PARAMETER = "retentionLocked"
PERIOD_PARAMETER = "retentionDays"
# 10 years from the end of the calendar year a file arrives in, plus the longest run to year
# end with leap days (ADR 0001, § 147 (4) AO).
MIN_RETENTION_DAYS = 4018
SKU_ALIAS = "Microsoft.CognitiveServices/accounts/deployments/sku.name"
# Point in time restore: 35 days is the most Azure allows for PostgreSQL Flexible Server (#82).
DATABASE_BACKUP_DAYS = 35
DATABASE_SERVER = "Microsoft.DBforPostgreSQL/flexibleServers"
BACKUP_VAULT = "Microsoft.DataProtection/backupVaults"
BACKUP_POLICY = "Microsoft.DataProtection/backupVaults/backupPolicies"
BACKUP_YEARS_PARAMETER = "databaseBackupYears"
DEMO_PARAMETER = "demo"
CONTAINER_APP = "Microsoft.App/containerApps"
CONTAINER_JOB = "Microsoft.App/jobs"
# The one module whose app may be reached from the internet: the ARGUS web app (ADR 0001).
PUBLIC_APP_MODULE = "apps"
STANDINS_MODULE = "standins"
# The scheduled intake jobs (#125) and the log lines their alerts look for.
INTAKE_COMMAND = "argus.intake"
INTAKE_SCHEDULES = {"sweep": "*/5 * * * *"}
INTAKE_DAILY = ("removal",)
# Sign in with Microsoft (#23, ADR 0008, #133). The web app needs all five, from a parameter.
SIGN_IN_SETTINGS = (
    "ARGUS_ENTRA_TENANT_ID",
    "ARGUS_ENTRA_API_CLIENT_ID",
    "ARGUS_ENTRA_GROUP_APPROVER",
    "ARGUS_ENTRA_GROUP_FINANCE",
    "ARGUS_ENTRA_GROUP_ADMIN",
)
# Points ARGUS at another sign in service. Only for the local fake (standins/entra).
FAKE_AUTHORITY_SETTING = "ARGUS_ENTRA_AUTHORITY"
ACTION_GROUP = "Microsoft.Insights/actionGroups"
LOG_ALERT = "Microsoft.Insights/scheduledQueryRules"
ALERT_PROCESSING_RULE = "Microsoft.AlertsManagement/actionRules"
DIAGNOSTIC_SETTING = "Microsoft.Insights/diagnosticSettings"
# Action groups and alert processing rules exist only as global resources. The group holds the
# address and sends the email, the rule holds a filter and a link to the group (#115).
GLOBAL_TYPES = frozenset({ACTION_GROUP, ALERT_PROCESSING_RULE})
# The table the vault writes its job records to, read by the "no backup" alert (#115).
BACKUP_JOBS_TABLE = "AddonAzureBackupJobs"
_WEEKLY_START = re.compile(r"^R/(\d{4}-\d{2}-\d{2})T[^/]+/P1W$")
_KUSTO_DAY = re.compile(r"dayofweek\(now\(\)\)\s*==\s*(\d)d")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_COPY_VARIABLE = re.compile(r"variables\('(\w+)'\)")
# 10 years for the long term backups until the tax advisor confirms the period (ADR 0007).
MIN_BACKUP_YEARS = 10

# Things that look like a real credential written down as plain text.
CREDENTIAL = re.compile(
    r"AccountKey=[A-Za-z0-9+/]{20,}"
    r"|SharedAccessSignature=|sig=[A-Za-z0-9%]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def resources(template: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every resource in a template and in the templates of its modules.

    Bicep writes resources as a list, or as an object keyed by name when the template uses
    newer language features, so both shapes are read.
    """
    found = template.get("resources", [])
    items = found.values() if isinstance(found, dict) else found
    for resource in items:
        yield resource
        nested = resource.get("properties", {}).get("template")
        if resource.get("type") == "Microsoft.Resources/deployments" and isinstance(nested, dict):
            yield from resources(nested)


def templates(template: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The template itself and the template of every module inside it."""
    yield template
    for resource in resources(template):
        nested = resource.get("properties", {}).get("template")
        if resource.get("type") == "Microsoft.Resources/deployments" and isinstance(nested, dict):
            yield nested


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def sku_lists(value: Any) -> Iterator[str]:
    """Every value of an allowedValues list that names a model deployment type."""
    if isinstance(value, dict):
        allowed = value.get("allowedValues")
        if isinstance(allowed, list) and any("Standard" in str(item) for item in allowed):
            yield from (str(item) for item in allowed)
        for item in value.values():
            yield from sku_lists(item)
    elif isinstance(value, list):
        for item in value:
            yield from sku_lists(item)


def ai_location_problems(module: dict[str, Any]) -> list[str]:
    """aiLocation may only place the AI accounts (ADR 0006). Everything else stays in the home
    region, and the Azure Policy alone would not catch it, since it allows every EU AI region
    (QA on PR 100, picked up in #81)."""
    name = module.get("name", "?")
    nested = module.get("properties", {}).get("template")
    placed = {
        resource.get("type")
        for resource in resources(nested if isinstance(nested, dict) else {})
        if "location" in resource
    }
    if AI_ACCOUNT not in placed:
        return [f"module {name} gets aiLocation but creates no AI account"]
    others = sorted(str(kind) for kind in placed - {AI_ACCOUNT})
    if others:
        return [f"module {name} gets aiLocation but also places {', '.join(others)}"]
    return []


def location_problems(main: dict[str, Any]) -> list[str]:
    problems = []
    location = main.get("parameters", {}).get("location", {})
    if (
        location.get("allowedValues") != [HOME_REGION]
        or location.get("defaultValue") != HOME_REGION
    ):
        problems.append(f"the location parameter must allow only {HOME_REGION}")
    ai_regions = main.get("parameters", {}).get("aiLocation", {}).get("allowedValues", [])
    if not ai_regions or not set(ai_regions) <= EU_REGIONS:
        problems.append(f"aiLocation must allow EU regions only, found {ai_regions}")
    for resource in resources(main):
        name = resource.get("name", "?")
        if resource.get("type") == "Microsoft.Resources/deployments":
            given = resource.get("properties", {}).get("parameters", {}).get("location")
            if given is not None and given.get("value") not in LOCATION_EXPRESSIONS:
                problems.append(f"module {name} gets a location that is not a region parameter")
            elif given is not None and given.get("value") == AI_LOCATION:
                problems.extend(ai_location_problems(resource))
        elif resource.get("type") in GLOBAL_TYPES and resource.get("location") == "global":
            continue
        elif "location" in resource and resource["location"] != "[parameters('location')]":
            problems.append(f"resource {name} has a fixed location {resource['location']!r}")
    return problems


def retention_problems(main: dict[str, Any], parameters: dict[str, Any] | None) -> list[str]:
    problems = []
    for template in templates(main):
        parameter = template.get("parameters", {}).get(LOCK_PARAMETER)
        if parameter is not None and parameter.get("defaultValue", False) is not False:
            problems.append(f"{LOCK_PARAMETER} must default to false")
        period = template.get("parameters", {}).get(PERIOD_PARAMETER)
        if period is not None:
            for key in ("defaultValue", "minValue"):
                if key in period and period[key] < MIN_RETENTION_DAYS:
                    problems.append(
                        f"{PERIOD_PARAMETER} {key} must be at least {MIN_RETENTION_DAYS} days"
                    )
            if "minValue" not in period:
                problems.append(f"{PERIOD_PARAMETER} needs a minValue of {MIN_RETENTION_DAYS}")
    if parameters is not None:
        given = parameters.get("parameters", {}).get(PERIOD_PARAMETER, {}).get("value")
        if given is not None and given < MIN_RETENTION_DAYS:
            problems.append(
                f"{PERIOD_PARAMETER} in the parameters file is below {MIN_RETENTION_DAYS}"
            )
    states = []
    for resource in resources(main):
        if resource.get("type") != "Microsoft.Storage/storageAccounts":
            continue
        policy = (
            resource.get("properties", {})
            .get("immutableStorageWithVersioning", {})
            .get("immutabilityPolicy")
        )
        if policy is None:
            continue
        state = policy.get("state", "")
        states.append(state)
        if f"parameters('{LOCK_PARAMETER}')" not in state:
            problems.append(
                f"the retention lock state must come from {LOCK_PARAMETER}, found {state!r}"
            )
    if not states:
        problems.append("no storage account has a retention policy for the original documents")
    return problems


def backup_problems(main: dict[str, Any], parameters: dict[str, Any] | None) -> list[str]:
    problems = []
    servers = [r for r in resources(main) if r.get("type") == DATABASE_SERVER]
    if not servers:
        problems.append("no PostgreSQL Flexible Server found")
    for server in servers:
        days = server.get("properties", {}).get("backup", {}).get("backupRetentionDays")
        if days != DATABASE_BACKUP_DAYS:
            problems.append(
                f"database server {server.get('name', '?')} keeps backups for {days} days, "
                f"not {DATABASE_BACKUP_DAYS}"
            )

    years = main.get("parameters", {}).get(BACKUP_YEARS_PARAMETER)
    if years is None:
        problems.append(f"main template has no {BACKUP_YEARS_PARAMETER} parameter")
    elif years.get("defaultValue", 0) < MIN_BACKUP_YEARS:
        problems.append(
            f"{BACKUP_YEARS_PARAMETER} must default to at least {MIN_BACKUP_YEARS} years"
        )
    if parameters is not None:
        given = parameters.get("parameters", {}).get(BACKUP_YEARS_PARAMETER, {}).get("value")
        if given is not None and given < MIN_BACKUP_YEARS:
            problems.append(
                f"{BACKUP_YEARS_PARAMETER} in the parameters file is below {MIN_BACKUP_YEARS}"
            )

    vaults = [r for r in resources(main) if r.get("type") == BACKUP_VAULT]
    if not vaults:
        problems.append("no Backup vault keeps long term backups of the database")
    for vault in vaults:
        name = vault.get("name", "?")
        security = vault.get("properties", {}).get("securitySettings", {})
        state = str(security.get("immutabilitySettings", {}).get("state", ""))
        # Immutable from the start: Unlocked until the lock, then Locked (ADR 0007).
        if (
            f"parameters('{LOCK_PARAMETER}')" not in state
            or "'Locked'" not in state
            or "'Unlocked'" not in state
        ):
            problems.append(
                f"backup vault {name} immutability must come from {LOCK_PARAMETER}, found {state!r}"
            )
        soft_delete = str(security.get("softDeleteSettings", {}).get("state", "Off"))
        if soft_delete == "Off" or "'Off'" in soft_delete:
            problems.append(f"backup vault {name} has soft delete off")

    kept_long = False
    for policy in (r for r in resources(main) if r.get("type") == BACKUP_POLICY):
        for rule in policy.get("properties", {}).get("policyRules", []):
            if rule.get("objectType") != "AzureRetentionRule":
                continue
            for lifecycle in rule.get("lifecycles", []):
                duration = str(lifecycle.get("deleteAfter", {}).get("duration", ""))
                if "parameters(" in duration:
                    kept_long = True
    if vaults and not kept_long:
        problems.append(
            "no backup policy keeps backups for a period set by a parameter "
            f"({BACKUP_YEARS_PARAMETER})"
        )
    return problems


def _resolve(template: dict[str, Any], value: Any) -> Any:
    """A plain variable reference read from the template's variables, anything else as is."""
    found = re.fullmatch(r"\[variables\('(\w+)'\)\]", value) if isinstance(value, str) else None
    return template.get("variables", {}).get(found[1], value) if found else value


def _backup_weekday(main: dict[str, Any]) -> int | None:
    """The weekday of the weekly backup, Sunday 0 as in Kusto, or None when not readable."""
    for template in templates(main):
        found = template.get("resources", [])
        for resource in found.values() if isinstance(found, dict) else found:
            if resource.get("type") != BACKUP_POLICY:
                continue
            for rule in resource.get("properties", {}).get("policyRules", []):
                schedule = rule.get("trigger", {}).get("schedule", {})
                for interval in schedule.get("repeatingTimeIntervals", []):
                    start = _WEEKLY_START.match(str(_resolve(template, interval)))
                    if start:
                        return (date.fromisoformat(start[1]).isoweekday()) % 7
    return None


def backup_alert_problems(main: dict[str, Any]) -> list[str]:
    """A failed backup job and a week without a completed backup both reach an action group
    (#115, NF-01)."""
    problems = []
    vaults = [r for r in resources(main) if r.get("type") == BACKUP_VAULT]
    for vault in vaults:
        setting = (
            vault.get("properties", {})
            .get("monitoringSettings", {})
            .get("azureMonitorAlertSettings", {})
            .get("alertsForAllJobFailures")
        )
        if setting != "Enabled":
            problems.append(
                f"backup vault {vault.get('name', '?')} raises no alert when a backup job fails"
            )
    if not vaults:
        return problems

    routed = False
    for rule in (r for r in resources(main) if r.get("type") == ALERT_PROCESSING_RULE):
        properties = rule.get("properties", {})
        backup = any(
            c.get("field") == "MonitorService"
            and c.get("operator") == "Equals"
            and "Azure Backup" in c.get("values", [])
            for c in properties.get("conditions", [])
        )
        sends = any(
            a.get("actionType") == "AddActionGroups" and a.get("actionGroupIds")
            for a in properties.get("actions", [])
        )
        routed = routed or (backup and sends and properties.get("enabled", True) is not False)
    if not routed:
        problems.append(
            "no alert processing rule sends the Azure Backup alerts (a failed backup job) to an "
            "action group"
        )

    logged = any(
        r.get("type") == DIAGNOSTIC_SETTING
        and "backupVaults" in str(r.get("scope", ""))
        and r.get("properties", {}).get("workspaceId")
        and any(
            log.get("category") == BACKUP_JOBS_TABLE and log.get("enabled")
            for log in r.get("properties", {}).get("logs", [])
        )
        for r in resources(main)
    )
    if not logged:
        problems.append(f"the backup vault sends no {BACKUP_JOBS_TABLE} records to Log Analytics")

    watchers = []
    for template in templates(main):
        found = template.get("resources", [])
        for resource in found.values() if isinstance(found, dict) else found:
            if resource.get("type") != LOG_ALERT:
                continue
            properties = resource.get("properties", {})
            for item in properties.get("criteria", {}).get("allOf", []):
                query = str(_resolve(template, item.get("query", "")))
                if BACKUP_JOBS_TABLE in query:
                    watchers.append((resource, query))
    if not watchers:
        problems.append("no alert fires when no backup job completed for 8 days")
    weekday = _backup_weekday(main)
    for resource, query in watchers:
        name = resource.get("name", "?")
        if not resource.get("properties", {}).get("actions", {}).get("actionGroups"):
            problems.append(f"alert {name} sends to no action group")
        day = _KUSTO_DAY.search(query)
        if weekday is None:
            problems.append("the weekly backup schedule cannot be read (R/<date>T<time>/P1W)")
        elif day is None or int(day[1]) != (weekday + 1) % 7:
            problems.append(
                f"alert {name} must check on the day after the weekly backup "
                f"(dayofweek(now()) == {(weekday + 1) % 7}d)"
            )
    return problems


def secret_problems(main: dict[str, Any], parameters: dict[str, Any] | None) -> list[str]:
    problems = []
    sources = [main] if parameters is None else [main, parameters]
    for source in sources:
        for text in strings(source):
            if CREDENTIAL.search(text):
                problems.append(f"something that looks like a credential: {text[:40]}...")
    for template in templates(main):
        for name, parameter in template.get("parameters", {}).items():
            if (
                parameter.get("type", "").lower().startswith("secure")
                and "defaultValue" in parameter
            ):
                problems.append(f"secure parameter {name} has a default value")
        for name, output in template.get("outputs", {}).items():
            if "listkeys(" in str(output.get("value", "")).lower():
                problems.append(f"output {name} hands out a key")
    for resource in resources(main):
        name = resource.get("name", "?")
        if resource.get("type") == "Microsoft.KeyVault/vaults/secrets":
            value = resource.get("properties", {}).get("value", "")
            if not (isinstance(value, str) and value.startswith("[")):
                problems.append(f"Key Vault secret {name} has a value written in the code")
        if resource.get("type") in (CONTAINER_APP, CONTAINER_JOB):
            configuration = resource.get("properties", {}).get("configuration", {})
            app_secrets = configuration.get("secrets", [])
            for secret in app_secrets if isinstance(app_secrets, list) else [app_secrets]:
                if "value" in secret or "keyVaultUrl" not in secret or "identity" not in secret:
                    problems.append(f"app {name} gets a secret that is not a Key Vault reference")
    return problems


def modules(template: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Each module of the main template: its name, its deployment resource and its template."""
    found = template.get("resources", [])
    items = found.values() if isinstance(found, dict) else found
    for resource in items:
        nested = resource.get("properties", {}).get("template")
        if resource.get("type") == "Microsoft.Resources/deployments" and isinstance(nested, dict):
            yield str(resource.get("name", "?")), resource, nested


def demo_problems(main: dict[str, Any], parameters: dict[str, Any] | None) -> list[str]:
    """The stand ins are for the demo only and never reachable from the internet (#124)."""
    problems = []
    demo = main.get("parameters", {}).get(DEMO_PARAMETER)
    if demo is None:
        problems.append(f"main template has no {DEMO_PARAMETER} parameter")
    elif demo.get("defaultValue", False) is not False:
        problems.append(f"{DEMO_PARAMETER} must default to false in the template")
    standins_found = False
    for name, deployment, template in modules(main):
        for resource in resources(template):
            if resource.get("type") != CONTAINER_APP:
                continue
            ingress = resource.get("properties", {}).get("configuration", {}).get("ingress", {})
            if name != PUBLIC_APP_MODULE and ingress.get("external", False) is not False:
                problems.append(f"module {name} has an app that the internet can reach")
        if name == STANDINS_MODULE:
            standins_found = True
            if f"parameters('{DEMO_PARAMETER}')" not in str(deployment.get("condition", "")):
                problems.append("the stand ins must only be deployed in demo mode")
    if not standins_found:
        problems.append(f"no {STANDINS_MODULE} module found")
    if parameters is not None:
        given = parameters.get("parameters", {})
        if given.get(DEMO_PARAMETER, {}).get("value") is True and given.get(
            "manualSecrets", {}
        ).get("value"):
            problems.append(
                "the parameters file switches demo mode on and also hands real secrets to "
                "ARGUS (manualSecrets); ARGUS would refuse to start"
            )
    return problems


def _job_items(template: dict[str, Any], resource: dict[str, Any]) -> list[dict[str, Any]]:
    """The items a job loop runs over, read from the template's plain variable, or the job
    itself when it is not a loop."""
    copy = resource.get("copy")
    if not copy:
        return [{}]
    found = _COPY_VARIABLE.search(str(copy.get("count", "")))
    items = template.get("variables", {}).get(found[1]) if found else None
    return items if isinstance(items, list) and all(isinstance(i, dict) for i in items) else []


def _daily(cron: str) -> bool:
    fields = cron.split()
    return len(fields) == 5 and all(f.isdigit() for f in fields[:2]) and fields[2:] == ["*"] * 3


def intake_job_problems(main: dict[str, Any]) -> list[str]:
    """The sweep runs every 5 minutes and the removal once a day, one run at a time and
    without retries, only when switched on, and every run is watched by an alert (#125)."""
    problems = []
    scheduled: dict[str, str] = {}
    for template in templates(main):
        found = template.get("resources", [])
        for resource in found.values() if isinstance(found, dict) else found:
            if resource.get("type") != CONTAINER_JOB:
                continue
            properties = resource.get("properties", {})
            containers = properties.get("template", {}).get("containers", [])
            command = " ".join(str(part) for c in containers for part in c.get("command", []))
            if INTAKE_COMMAND not in command:
                continue
            name = str(resource.get("name", "?"))
            configuration = properties.get("configuration", {})
            trigger = configuration.get("scheduleTriggerConfig", {})
            if configuration.get("triggerType") != "Schedule":
                problems.append(f"intake job {name} must run on a schedule")
            if configuration.get("replicaRetryLimit") != 0:
                problems.append(f"intake job {name} must not retry; the next run tries again")
            if trigger.get("parallelism") != 1 or trigger.get("replicaCompletionCount") != 1:
                problems.append(f"intake job {name} must run one replica at a time")
            if not configuration.get("replicaTimeout"):
                problems.append(f"intake job {name} needs a time limit per run")
            if "condition" not in resource:
                problems.append(f"intake job {name} must only run when the intake jobs are on")
            items = _job_items(template, resource)
            if not items:
                problems.append(f"intake job {name} must loop over a plain list of jobs")
            for item in items:
                job = str(item.get("job", ""))
                cron = str(item.get("cron", trigger.get("cronExpression", "")))
                if job not in command and "copyIndex()].job" not in command:
                    problems.append(f"intake job {name} does not run the job {job!r}")
                scheduled[job] = cron
    for job, cron in INTAKE_SCHEDULES.items():
        if scheduled.get(job) != cron:
            problems.append(f"the {job} job must run on the schedule {cron!r}")
    for job in INTAKE_DAILY:
        if not _daily(scheduled.get(job, "")):
            problems.append(f"the {job} job must run once a day")
    unknown = sorted(set(scheduled) - set(INTAKE_SCHEDULES) - set(INTAKE_DAILY))
    if unknown:
        problems.append(f"unknown intake jobs {unknown}: add their schedule to this check")
    problems.extend(alert_problems(main, sorted(scheduled)))
    return problems


def alert_problems(main: dict[str, Any], jobs: list[str]) -> list[str]:
    """Every intake job has a rule for a failed run and one for no run, each sending to an
    action group, and no alert address is written in the code (#125)."""
    problems = []
    queries: list[str] = []
    groups = 0
    for template in templates(main):
        found = template.get("resources", [])
        for resource in found.values() if isinstance(found, dict) else found:
            kind = resource.get("type")
            properties = resource.get("properties", {})
            if kind == ACTION_GROUP:
                groups += 1
                for text in strings(properties):
                    if not text.startswith("[") and EMAIL.search(text):
                        problems.append("an alert email address is written in the code")
            if kind != LOG_ALERT:
                continue
            name = str(resource.get("name", "?"))
            if not properties.get("actions", {}).get("actionGroups"):
                problems.append(f"alert {name} sends to no action group")
            items = _job_items(template, resource) if resource.get("copy") else [properties]
            for item in items:
                queries.append(str(item.get("query", "")))
                criteria = item.get("criteria", {}).get("allOf", [])
                queries.extend(str(c.get("query", "")) for c in criteria)
    if jobs and not groups:
        problems.append("no action group for the operations alerts")
    for job in jobs:
        for outcome in ("failed", "ok"):
            mark = f"argus-job {outcome} {job}"
            if not any(mark in query for query in queries):
                problems.append(f"no alert watches the log line {mark!r}")
    return problems


def deployment_type_problems(
    main: dict[str, Any], policy: dict[str, Any], parameters: dict[str, Any] | None
) -> list[str]:
    problems = []
    # Bicep may keep euDeploymentSku as its own definition, with a prefix such as "_1.", or
    # write it inline into modelDeployment, so every list of allowed SKUs is collected.
    allowed_by_type = set(sku_lists(main.get("definitions", {})))
    if not allowed_by_type or not allowed_by_type <= EU_DEPLOYMENT_SKUS:
        problems.append(
            f"euDeploymentSku must allow EU types only, found {sorted(allowed_by_type)}"
        )

    for resource in resources(main):
        if resource.get("type") == "Microsoft.CognitiveServices/accounts/deployments":
            sku = resource.get("sku", {}).get("name", "")
            if sku.startswith("Global"):
                problems.append(f"model deployment {resource.get('name')} uses {sku}")

    if parameters is not None:
        for deployment in (
            parameters.get("parameters", {}).get("modelDeployments", {}).get("value", [])
        ):
            if deployment.get("sku") not in EU_DEPLOYMENT_SKUS:
                problems.append(
                    f"model deployment {deployment.get('name')} uses {deployment.get('sku')}"
                )

    allowed_by_policy = set(
        policy.get("parameters", {}).get("allowedDeploymentSkus", {}).get("defaultValue", [])
    )
    if not allowed_by_policy or not allowed_by_policy <= EU_DEPLOYMENT_SKUS:
        problems.append(
            f"the policy must allow EU deployment types only, found {sorted(allowed_by_policy)}"
        )
    if allowed_by_type and allowed_by_policy and allowed_by_type != allowed_by_policy:
        problems.append("euDeploymentSku and the policy allow different deployment types")

    rule_found = False
    for resource in resources(policy):
        if resource.get("type") != "Microsoft.Authorization/policyDefinitions":
            continue
        rule = resource.get("properties", {}).get("policyRule", {})
        conditions = rule.get("if", {}).get("allOf", [])
        if any(c.get("field") == SKU_ALIAS and "notIn" in c for c in conditions):
            rule_found = True
            if str(rule.get("then", {}).get("effect", "")).lower() != "deny":
                problems.append("the deployment type policy must deny, not just audit")
    if not rule_found:
        problems.append("no policy refuses model deployment types outside the allowed list")

    ai_regions = set(
        policy.get("parameters", {}).get("allowedAiLocations", {}).get("defaultValue", [])
    )
    if not ai_regions or not ai_regions <= EU_REGIONS:
        problems.append(f"the policy must allow EU regions only, found {sorted(ai_regions)}")
    return problems


def _unconditional_variables(expression: str) -> list[str]:
    """The variables an ARM expression always uses, leaving out those inside an ``if(...)``."""
    found = []
    calls: list[str] = []
    word = ""
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "'":
            end = expression.find("'", index + 1)
            literal = expression[index + 1 : end if end != -1 else len(expression)]
            if calls and calls[-1] == "variables" and "if" not in calls:
                found.append(literal)
            index = len(expression) if end == -1 else end + 1
            word = ""
            continue
        if char.isalnum() or char == "_":
            word += char
        elif char == "(":
            calls.append(word.lower())
            word = ""
        elif char == ")":
            if calls:
                calls.pop()
            word = ""
        else:
            word = ""
        index += 1
    return found


def _env_entries(template: dict[str, Any], env: Any) -> list[dict[str, Any]]:
    """The name and value entries a container always gets, as far as they can be read."""
    if isinstance(env, list):
        return [entry for entry in env if isinstance(entry, dict)]
    if not isinstance(env, str):
        return []
    variables = template.get("variables", {})
    entries: list[dict[str, Any]] = []
    for name in _unconditional_variables(env):
        value = variables.get(name)
        if isinstance(value, list):
            entries.extend(entry for entry in value if isinstance(entry, dict))
    return entries


def sign_in_problems(main: dict[str, Any]) -> list[str]:
    """The web app always gets the five sign in settings from a parameter, and the setting for
    the local fake sign in service is never set in Azure (#23, #133, NF-03)."""
    problems = []
    if any(FAKE_AUTHORITY_SETTING in text for text in strings(main)):
        problems.append(
            f"{FAKE_AUTHORITY_SETTING} is for the local fake only and must stay unset in Azure"
        )
    web_apps = 0
    for name, _deployment, template in modules(main):
        if name != PUBLIC_APP_MODULE:
            continue
        for resource in resources(template):
            if resource.get("type") != CONTAINER_APP:
                continue
            web_apps += 1
            app = resource.get("name", "?")
            containers = resource.get("properties", {}).get("template", {}).get("containers", [])
            entries = [
                entry
                for container in containers
                if isinstance(container, dict)
                for entry in _env_entries(template, container.get("env"))
            ]
            given = {entry.get("name"): entry.get("value") for entry in entries}
            for setting in SIGN_IN_SETTINGS:
                if setting not in given:
                    problems.append(f"app {app} does not always get the sign in setting {setting}")
                elif "parameters(" not in str(given[setting]):
                    problems.append(
                        f"app {app} gets the sign in setting {setting} as a fixed value; it must "
                        "come from a parameter (GitHub variables)"
                    )
    if web_apps == 0:
        problems.append(f"no ARGUS web app found in the {PUBLIC_APP_MODULE} module")
    return problems


def check(folder: Path) -> list[str]:
    main = json.loads((folder / "main.json").read_text(encoding="utf-8"))
    policy = json.loads((folder / "eu-only.json").read_text(encoding="utf-8"))
    parameters_file = folder / "main.parameters.json"
    parameters = (
        json.loads(parameters_file.read_text(encoding="utf-8"))
        if parameters_file.exists()
        else None
    )
    return [
        *location_problems(main),
        *retention_problems(main, parameters),
        *backup_problems(main, parameters),
        *backup_alert_problems(main),
        *secret_problems(main, parameters),
        *deployment_type_problems(main, policy, parameters),
        *demo_problems(main, parameters),
        *intake_job_problems(main),
        *sign_in_problems(main),
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python tools/check_infra.py <folder with main.json and eu-only.json>")
        return 2
    problems = check(Path(argv[0]))
    for problem in problems:
        print(f"Infrastructure problem: {problem}")
    if problems:
        return 1
    print(
        "Infrastructure code keeps to the rules of #65, #82, #115, #124, #125 and #133 "
        "(NF-01, NF-03, NF-06)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
