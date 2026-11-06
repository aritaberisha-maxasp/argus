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
  ``retentionLocked``, or soft delete is off (#82, ADR 0007, NF-01);
- a secret could end up in the code, a parameter default or a deployment output, or the app
  gets a secret any other way than from Key Vault (NF-06);
- a model deployment, the allowed deployment types or the Azure Policy would let a Global
  deployment type through (ADR 0006).

Run from the repository root: ``python tools/check_infra.py build/infra``
"""

import json
import re
import sys
from collections.abc import Iterator
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
        if f"parameters('{LOCK_PARAMETER}')" not in state:
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
        if resource.get("type") == "Microsoft.App/containerApps":
            configuration = resource.get("properties", {}).get("configuration", {})
            app_secrets = configuration.get("secrets", [])
            for secret in app_secrets if isinstance(app_secrets, list) else [app_secrets]:
                if "value" in secret or "keyVaultUrl" not in secret or "identity" not in secret:
                    problems.append(f"app {name} gets a secret that is not a Key Vault reference")
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
        *secret_problems(main, parameters),
        *deployment_type_problems(main, policy, parameters),
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
    print("Infrastructure code keeps to the rules of #65 and #82 (NF-01, NF-03, NF-06).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
