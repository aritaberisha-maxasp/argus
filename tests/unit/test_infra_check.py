"""The CI check on the compiled Azure infrastructure code (#65, NF-01, NF-03, NF-06).

The real templates are compiled and checked in the CI job "Infrastructure code". These tests
prove the check itself catches each broken rule, on small synthetic templates.
"""

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

# Built at runtime from repeated letters, so the secret scan does not take it for a real key.
FAKE_ACCOUNT_KEY = "AccountKey=" + "A" * 40 + "=="
# How Bicep compiles 'P${retentionYears}Y' in infra/modules/backup.bicep.
YEARS_EXPRESSION = "[format('P{0}Y', parameters('retentionYears'))]"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_infra", REPO / "tools" / "check_infra.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_infra = load_check()


def module(name: str, location: str, template: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Microsoft.Resources/deployments",
        "name": name,
        "properties": {
            "parameters": {"location": {"value": location}},
            "template": template,
        },
    }


def intake_jobs() -> dict[str, Any]:
    """The sweep and removal jobs as Bicep compiles the loop in infra/modules/apps.bicep."""
    return {
        "copy": {"name": "intakeJobs", "count": "[length(variables('intakeJobList'))]"},
        "condition": "[parameters('runIntakeJobs')]",
        "type": "Microsoft.App/jobs",
        "name": "[parameters('intakeJobNames')[variables('intakeJobList')[copyIndex()].job]]",
        "location": "[parameters('location')]",
        "properties": {
            "configuration": {
                "triggerType": "Schedule",
                "replicaTimeout": 1800,
                "replicaRetryLimit": 0,
                "scheduleTriggerConfig": {
                    "cronExpression": "[variables('intakeJobList')[copyIndex()].cron]",
                    "parallelism": 1,
                    "replicaCompletionCount": 1,
                },
                "secrets": [
                    {
                        "name": "azure-openai-api-key",
                        "keyVaultUrl": "[format('{0}secrets/x', parameters('kv'))]",
                        "identity": "[parameters('identityId')]",
                    }
                ],
            },
            "template": {
                "containers": [
                    {
                        "name": "[variables('intakeJobList')[copyIndex()].job]",
                        "command": [
                            "python",
                            "-m",
                            "argus.intake",
                            "[variables('intakeJobList')[copyIndex()].job]",
                        ],
                    }
                ]
            },
        },
    }


# How Bicep compiles the env list of the audit copy job in infra/modules/apps.bicep (#103).
AUDIT_JOB_ENV = (
    "[concat(variables('plainSettings'), map(variables('auditSecrets'), lambda('secret', "
    "createObject('name', lambdaVariables('secret').envName, 'secretRef', "
    "toLower(lambdaVariables('secret').secretName)))))]"
)


def audit_job() -> dict[str, Any]:
    """The nightly audit copy job as Bicep compiles it from infra/modules/apps.bicep, with its
    secrets written as a loop (#103)."""
    return {
        "condition": "[parameters('runAuditJob')]",
        "type": "Microsoft.App/jobs",
        "name": "[parameters('auditJobName')]",
        "location": "[parameters('location')]",
        "properties": {
            "configuration": {
                "copy": [
                    {
                        "name": "secrets",
                        "count": "[length(variables('auditSecrets'))]",
                        "input": {
                            "name": "[toLower(variables('auditSecrets')[copyIndex('secrets')]"
                            ".secretName)]",
                            "keyVaultUrl": "[format('{0}secrets/{1}', parameters('keyVaultUri'),"
                            " variables('auditSecrets')[copyIndex('secrets')].secretName)]",
                            "identity": "[parameters('identityId')]",
                        },
                    }
                ],
                "triggerType": "Schedule",
                "replicaTimeout": 3600,
                "replicaRetryLimit": 0,
                "scheduleTriggerConfig": {
                    "cronExpression": "[variables('auditJob').cron]",
                    "parallelism": 1,
                    "replicaCompletionCount": 1,
                },
            },
            "template": {
                "containers": [
                    {
                        "name": "[variables('auditJob').job]",
                        "command": ["python", "-m", "argus.audit"],
                        "env": AUDIT_JOB_ENV,
                    }
                ]
            },
        },
    }


def alerts() -> dict[str, Any]:
    """The action group and the log alert loop as compiled from infra/modules/alerts.bicep."""
    rules = [
        {"job": job, "kind": kind, "query": f'Logs | where Log_s contains "argus-job {mark} {job}"'}
        for job in ("sweep", "removal")
        for kind, mark in (("failed", "failed"), ("silent", "ok"))
    ]
    audit_rules = [
        {"kind": kind, "query": f'Logs | where Log_s contains "argus-job {mark} audit-copy"'}
        for kind, mark in (("failed", "failed"), ("silent", "ok"))
    ]
    return {
        "parameters": {"location": {"type": "string"}, "alertEmail": {"type": "string"}},
        "variables": {"intakeRules": rules, "auditRules": audit_rules},
        "resources": {
            "actionGroup": {
                "type": "Microsoft.Insights/actionGroups",
                "name": "ag-argus-ops",
                "location": "global",
                "properties": {
                    "emailReceivers": "[if(empty(parameters('alertEmail')), createArray(),"
                    " createArray(createObject('emailAddress', parameters('alertEmail'))))]"
                },
            },
            "intakeAlerts": {
                "copy": {"name": "intakeAlerts", "count": "[length(variables('intakeRules'))]"},
                "condition": "[parameters('watchIntakeJobs')]",
                "type": "Microsoft.Insights/scheduledQueryRules",
                "name": "[format('alert-{0}', variables('intakeRules')[copyIndex()].kind)]",
                "location": "[parameters('location')]",
                "properties": {
                    "criteria": {
                        "allOf": [{"query": "[variables('intakeRules')[copyIndex()].query]"}]
                    },
                    "actions": {"actionGroups": ["[resourceId('x', 'ag-argus-ops')]"]},
                },
            },
            "auditAlerts": {
                "copy": {"name": "auditAlerts", "count": "[length(variables('auditRules'))]"},
                "condition": "[parameters('watchAuditJob')]",
                "type": "Microsoft.Insights/scheduledQueryRules",
                "name": "[format('alert-audit-{0}', variables('auditRules')[copyIndex()].kind)]",
                "location": "[parameters('location')]",
                "properties": {
                    "criteria": {
                        "allOf": [{"query": "[variables('auditRules')[copyIndex()].query]"}]
                    },
                    "actions": {"actionGroups": ["[resourceId('x', 'ag-argus-ops')]"]},
                },
            },
        },
    }


# How Bicep compiles the env list of the web app in infra/modules/apps.bicep (#133).
WEB_APP_ENV = (
    "[concat(variables('plainSettings'), variables('signInEnv'), if(parameters('demo'), "
    "variables('demoSettings'), createArray()), map(variables('allSecrets'), lambda('secret', "
    "createObject('name', lambdaVariables('secret').envName, 'secretRef', "
    "toLower(lambdaVariables('secret').secretName)))))]"
)
SIGN_IN_FIELDS = {
    "ARGUS_ENTRA_TENANT_ID": "tenantId",
    "ARGUS_ENTRA_API_CLIENT_ID": "apiClientId",
    "ARGUS_ENTRA_GROUP_APPROVER": "approverGroupId",
    "ARGUS_ENTRA_GROUP_FINANCE": "financeGroupId",
    "ARGUS_ENTRA_GROUP_ADMIN": "adminGroupId",
}


def sign_in_env() -> list[dict[str, str]]:
    return [
        {"name": name, "value": f"[parameters('signInSettings').{field}]"}
        for name, field in SIGN_IN_FIELDS.items()
    ]


def web_app(main: dict[str, Any]) -> dict[str, Any]:
    app: dict[str, Any] = main["resources"]["apps"]["properties"]["template"]["resources"][1]
    return app


def apps_variables(main: dict[str, Any]) -> dict[str, Any]:
    variables: dict[str, Any] = main["resources"]["apps"]["properties"]["template"]["variables"]
    return variables


def good_main() -> dict[str, Any]:
    documents = {
        "parameters": {
            "location": {"type": "string"},
            "retentionLocked": {"type": "bool", "defaultValue": False},
            "retentionDays": {"type": "int", "minValue": 4018},
        },
        "resources": {
            "account": {
                "type": "Microsoft.Storage/storageAccounts",
                "name": "stargusdocs",
                "location": "[parameters('location')]",
                "properties": {
                    "immutableStorageWithVersioning": {
                        "enabled": True,
                        "immutabilityPolicy": {
                            "state": "[if(parameters('retentionLocked'), 'Locked', 'Unlocked')]"
                        },
                    }
                },
            },
            "secret": {
                "type": "Microsoft.KeyVault/vaults/secrets",
                "name": "kv/argus-documents-connection-string",
                "properties": {
                    "value": "[format('AccountKey={0}', listKeys('x', 'y').keys[0].value)]"
                },
            },
        },
        "outputs": {"name": {"type": "string", "value": "[parameters('location')]"}},
    }
    ai = {
        "parameters": {"location": {"type": "string"}},
        "resources": [
            {
                "type": "Microsoft.CognitiveServices/accounts",
                "name": "oai",
                "location": "[parameters('location')]",
            },
            {
                "type": "Microsoft.CognitiveServices/accounts/deployments",
                "name": "[format('{0}/{1}', 'oai', 'argus-sorting')]",
                "sku": {"name": "[parameters('deployments')[0].sku]"},
            },
        ],
    }
    apps = {
        "parameters": {"location": {"type": "string"}},
        "variables": {
            "intakeJobList": [
                {"job": "sweep", "cron": "*/5 * * * *"},
                {"job": "removal", "cron": "30 2 * * *"},
            ],
            "plainSettings": [
                {"name": "ARGUS_DEMO", "value": "[if(parameters('demo'), 'true', 'false')]"}
            ],
            "signInEnv": sign_in_env(),
            "demoSettings": [{"name": "ARGUS_MAILBOX", "value": "accounts@maxasp.example"}],
            "auditJob": {"job": "audit-copy", "cron": "0 1 * * *"},
            "auditSecrets": ["[variables('generatedSecrets')[0]]"],
        },
        "resources": [
            intake_jobs(),
            {
                "type": "Microsoft.App/containerApps",
                "name": "ca-argus-web",
                "location": "[parameters('location')]",
                "properties": {
                    "template": {"containers": [{"name": "argus", "env": WEB_APP_ENV}]},
                    "configuration": {
                        "secrets": [
                            {
                                "name": "azure-openai-api-key",
                                "keyVaultUrl": "[format('{0}secrets/x', parameters('kv'))]",
                                "identity": "[parameters('identityId')]",
                            }
                        ]
                    },
                },
            },
            audit_job(),
        ],
    }
    # Each stand in reachable only inside the Container Apps environment (#124).
    standins = {
        "parameters": {"location": {"type": "string"}},
        "resources": {
            "apps": {
                "copy": {"name": "apps", "count": 3},
                "type": "Microsoft.App/containerApps",
                "name": "[variables('standins')[copyIndex()].name]",
                "location": "[parameters('location')]",
                "properties": {
                    "configuration": {
                        "ingress": {"external": False, "allowInsecure": True},
                        "secrets": [],
                    }
                },
            },
            "seedJob": {
                "type": "Microsoft.App/jobs",
                "name": "caj-argus-demo-seed",
                "location": "[parameters('location')]",
                "properties": {
                    "configuration": {
                        "triggerType": "Manual",
                        "secrets": [
                            {
                                "name": "argus-graph-standin-token",
                                "keyVaultUrl": "[format('{0}secrets/x', parameters('kv'))]",
                                "identity": "[parameters('identityId')]",
                            }
                        ],
                    }
                },
            },
        },
    }
    database = {
        "parameters": {"location": {"type": "string"}},
        "resources": {
            "server": {
                "type": "Microsoft.DBforPostgreSQL/flexibleServers",
                "name": "psql-argus",
                "location": "[parameters('location')]",
                "properties": {
                    "backup": {"backupRetentionDays": 35, "geoRedundantBackup": "Disabled"}
                },
            }
        },
    }
    backup = {
        "parameters": {
            "location": {"type": "string"},
            "retentionYears": {"type": "int", "maxValue": 10},
            "retentionLocked": {"type": "bool"},
        },
        "resources": {
            "vault": {
                "type": "Microsoft.DataProtection/backupVaults",
                "name": "bvault-argus",
                "location": "[parameters('location')]",
                "properties": {
                    "storageSettings": [{"datastoreType": "VaultStore", "type": "GeoRedundant"}],
                    "securitySettings": {
                        "immutabilitySettings": {
                            "state": "[if(parameters('retentionLocked'), 'Locked', 'Unlocked')]"
                        },
                        "softDeleteSettings": {
                            "state": "[if(parameters('retentionLocked'), 'AlwaysOn', 'On')]",
                            "retentionDurationInDays": 14,
                        },
                    },
                },
            },
            "policy": {
                "type": "Microsoft.DataProtection/backupVaults/backupPolicies",
                "name": "[format('{0}/{1}', 'bvault-argus', 'argus-database-monthly')]",
                "properties": {
                    "policyRules": [
                        {"name": "BackupWeekly", "objectType": "AzureBackupRule"},
                        {
                            "name": "Default",
                            "objectType": "AzureRetentionRule",
                            "lifecycles": [{"deleteAfter": {"duration": "P3M"}}],
                        },
                        {
                            "name": "Monthly",
                            "objectType": "AzureRetentionRule",
                            "lifecycles": [{"deleteAfter": {"duration": YEARS_EXPRESSION}}],
                        },
                    ]
                },
            },
        },
    }
    return {
        "definitions": {
            "_1.euDeploymentSku": {
                "type": "string",
                "allowedValues": ["DataZoneStandard", "Standard"],
            }
        },
        "parameters": {
            "location": {
                "type": "string",
                "defaultValue": "germanywestcentral",
                "allowedValues": ["germanywestcentral"],
            },
            "aiLocation": {
                "type": "string",
                "allowedValues": ["germanywestcentral", "swedencentral"],
            },
            "retentionLocked": {"type": "bool", "defaultValue": False},
            "retentionDays": {"type": "int", "defaultValue": 4018, "minValue": 4018},
            "databaseBackupYears": {
                "type": "int",
                "defaultValue": 10,
                "minValue": 1,
                "maxValue": 10,
            },
            "demo": {"type": "bool", "defaultValue": False},
            "demoImage": {"type": "string", "defaultValue": ""},
        },
        "resources": {
            "identity": {
                "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
                "name": "id-argus-app",
                "location": "[parameters('location')]",
            },
            "documents": module("documents", "[parameters('location')]", documents),
            "database": module("database", "[parameters('location')]", database),
            "backup": module("backup", "[parameters('location')]", backup),
            "ai": module("ai", "[parameters('aiLocation')]", ai),
            "apps": module("apps", "[parameters('location')]", apps),
            "alerts": module("alerts", "[parameters('location')]", alerts()),
            "standins": {
                **module("standins", "[parameters('location')]", standins),
                "condition": "[and(parameters('demo'), not(empty(parameters('demoImage'))))]",
            },
        },
        "outputs": {
            "appUrl": {"type": "string", "value": "[reference('apps').outputs.appUrl.value]"}
        },
    }


def good_policy() -> dict[str, Any]:
    return {
        "parameters": {
            "allowedDeploymentSkus": {
                "type": "array",
                "defaultValue": ["DataZoneStandard", "Standard"],
            },
            "allowedAiLocations": {
                "type": "array",
                "defaultValue": ["germanywestcentral", "swedencentral"],
            },
        },
        "resources": [
            {
                "type": "Microsoft.Authorization/policyDefinitions",
                "name": "argus-ai-eu-deployment-types",
                "properties": {
                    "policyRule": {
                        "if": {
                            "allOf": [
                                {
                                    "field": "type",
                                    "equals": "Microsoft.CognitiveServices/accounts/deployments",
                                },
                                {
                                    "field": check_infra.SKU_ALIAS,
                                    "notIn": "[parameters('allowedSkus')]",
                                },
                            ]
                        },
                        "then": {"effect": "deny"},
                    }
                },
            }
        ],
    }


def good_parameters() -> dict[str, Any]:
    return {
        "parameters": {
            "retentionLocked": {"value": False},
            "retentionDays": {"value": 4018},
            "databaseBackupYears": {"value": 10},
            "modelDeployments": {"value": [{"name": "argus-sorting", "sku": "DataZoneStandard"}]},
            "demo": {"value": True},
            "manualSecrets": {"value": []},
        }
    }


def run(
    tmp_path: Path,
    main: dict[str, Any],
    policy: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
) -> list[str]:
    (tmp_path / "main.json").write_text(json.dumps(main), encoding="utf-8")
    (tmp_path / "eu-only.json").write_text(json.dumps(policy or good_policy()), encoding="utf-8")
    (tmp_path / "main.parameters.json").write_text(
        json.dumps(parameters or good_parameters()), encoding="utf-8"
    )
    problems: list[str] = check_infra.check(tmp_path)
    return problems


@pytest.mark.req("NF-01", "NF-03", "NF-06")
def test_templates_that_keep_the_rules_pass(tmp_path: Path) -> None:
    assert run(tmp_path, good_main()) == []
    assert check_infra.main([str(tmp_path)]) == 0


@pytest.mark.req("NF-03")
def test_a_second_home_region_fails(tmp_path: Path) -> None:
    main = good_main()
    main["parameters"]["location"]["allowedValues"].append("eastus")
    assert any("location parameter" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_a_fixed_region_on_a_resource_fails(tmp_path: Path) -> None:
    main = good_main()
    main["resources"]["identity"]["location"] = "westus"
    assert any("fixed location" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_a_module_with_a_fixed_region_fails(tmp_path: Path) -> None:
    main = good_main()
    main["resources"]["ai"]["properties"]["parameters"]["location"]["value"] = "eastus"
    assert any("module ai" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_an_ai_region_outside_the_eu_fails(tmp_path: Path) -> None:
    main = good_main()
    main["parameters"]["aiLocation"]["allowedValues"].append("eastus2")
    assert any("aiLocation" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("module_name", ["documents", "apps"])
def test_the_ai_region_on_a_module_without_ai_accounts_fails(
    tmp_path: Path, module_name: str
) -> None:
    main = good_main()
    main["resources"][module_name]["properties"]["parameters"]["location"]["value"] = (
        "[parameters('aiLocation')]"
    )
    assert any(f"module {module_name} gets aiLocation" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_the_ai_module_may_not_place_anything_else_in_the_ai_region(tmp_path: Path) -> None:
    main = good_main()
    main["resources"]["ai"]["properties"]["template"]["resources"].append(
        {
            "type": "Microsoft.Storage/storageAccounts",
            "name": "stsidecar",
            "location": "[parameters('location')]",
        }
    )
    problems = run(tmp_path, main)
    assert any("also places Microsoft.Storage/storageAccounts" in p for p in problems)


@pytest.mark.req("NF-01")
def test_the_retention_lock_on_by_default_fails(tmp_path: Path) -> None:
    main = good_main()
    main["parameters"]["retentionLocked"]["defaultValue"] = True
    assert any("default to false" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_a_module_that_locks_by_default_fails(tmp_path: Path) -> None:
    main = good_main()
    documents = main["resources"]["documents"]["properties"]["template"]
    documents["parameters"]["retentionLocked"]["defaultValue"] = True
    assert any("default to false" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("key", ["defaultValue", "minValue"])
def test_a_period_shorter_than_ten_years_from_year_end_fails(tmp_path: Path, key: str) -> None:
    main = good_main()
    main["parameters"]["retentionDays"][key] = 3653
    assert any(f"retentionDays {key} must be at least 4018" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_a_module_period_without_a_floor_fails(tmp_path: Path) -> None:
    main = good_main()
    documents = main["resources"]["documents"]["properties"]["template"]
    del documents["parameters"]["retentionDays"]["minValue"]
    assert any("needs a minValue" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_a_short_period_in_the_parameters_fails(tmp_path: Path) -> None:
    parameters = good_parameters()
    parameters["parameters"]["retentionDays"]["value"] = 3653
    problems = run(tmp_path, good_main(), parameters=parameters)
    assert any("parameters file is below 4018" in p for p in problems)


@pytest.mark.req("NF-01")
def test_a_lock_written_straight_into_the_code_fails(tmp_path: Path) -> None:
    main = good_main()
    account = main["resources"]["documents"]["properties"]["template"]["resources"]["account"]
    account["properties"]["immutableStorageWithVersioning"]["immutabilityPolicy"]["state"] = (
        "Locked"
    )
    assert any("must come from retentionLocked" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_no_retention_policy_at_all_fails(tmp_path: Path) -> None:
    main = good_main()
    account = main["resources"]["documents"]["properties"]["template"]["resources"]["account"]
    del account["properties"]["immutableStorageWithVersioning"]
    assert any("no storage account" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-06")
@pytest.mark.parametrize(
    "text",
    [
        FAKE_ACCOUNT_KEY,
        "eyJ" + "x" * 12 + "." + "y" * 12 + ".signature",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
    ],
)
def test_a_credential_in_the_code_fails(tmp_path: Path, text: str) -> None:
    main = good_main()
    main["resources"]["identity"]["tags"] = {"note": text}
    assert any("looks like a credential" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-06")
def test_a_credential_in_the_parameters_fails(tmp_path: Path) -> None:
    parameters = good_parameters()
    parameters["parameters"]["wiseToken"] = {"value": FAKE_ACCOUNT_KEY}
    assert any(
        "looks like a credential" in p for p in run(tmp_path, good_main(), parameters=parameters)
    )


@pytest.mark.req("NF-06")
def test_a_secure_parameter_with_a_default_fails(tmp_path: Path) -> None:
    main = good_main()
    main["parameters"]["wiseToken"] = {"type": "securestring", "defaultValue": "x"}
    assert any("secure parameter wiseToken" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-06")
def test_an_output_that_hands_out_a_key_fails(tmp_path: Path) -> None:
    main = good_main()
    main["outputs"]["key"] = {"type": "string", "value": "[listKeys('x', 'y').key1]"}
    assert any("hands out a key" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-06")
def test_a_key_vault_secret_written_in_the_code_fails(tmp_path: Path) -> None:
    main = good_main()
    secret = main["resources"]["documents"]["properties"]["template"]["resources"]["secret"]
    secret["properties"]["value"] = "plain text"
    assert any("written in the code" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-06")
@pytest.mark.parametrize(
    "secret",
    [
        {"name": "wise", "value": "[parameters('wise')]"},
        {"name": "wise", "keyVaultUrl": "https://kv/secrets/wise"},
    ],
)
def test_an_app_secret_that_is_not_read_from_key_vault_fails(
    tmp_path: Path, secret: dict[str, str]
) -> None:
    main = good_main()
    app = main["resources"]["apps"]["properties"]["template"]["resources"][0]
    app["properties"]["configuration"]["secrets"].append(secret)
    assert any("not a Key Vault reference" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("sku", ["GlobalStandard", "GlobalProvisionedManaged", "GlobalBatch"])
def test_a_global_type_in_the_allowed_types_fails(tmp_path: Path, sku: str) -> None:
    main = good_main()
    main["definitions"]["_1.euDeploymentSku"]["allowedValues"].append(sku)
    assert any("euDeploymentSku" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_the_allowed_types_found_inline_are_checked_too(tmp_path: Path) -> None:
    main = good_main()
    main["definitions"] = {
        "modelDeployment": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "allowedValues": ["DataZoneStandard", "GlobalStandard"]}
            },
        }
    }
    assert any("euDeploymentSku" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_a_global_deployment_in_the_parameters_fails(tmp_path: Path) -> None:
    parameters = good_parameters()
    parameters["parameters"]["modelDeployments"]["value"][0]["sku"] = "GlobalStandard"
    problems = run(tmp_path, good_main(), parameters=parameters)
    assert any("argus-sorting uses GlobalStandard" in p for p in problems)


@pytest.mark.req("NF-03")
def test_a_global_deployment_written_in_the_template_fails(tmp_path: Path) -> None:
    main = good_main()
    deployment = main["resources"]["ai"]["properties"]["template"]["resources"][1]
    deployment["sku"]["name"] = "GlobalStandard"
    assert any("uses GlobalStandard" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-03")
def test_a_policy_that_allows_a_global_type_fails(tmp_path: Path) -> None:
    policy = good_policy()
    policy["parameters"]["allowedDeploymentSkus"]["defaultValue"].append("GlobalStandard")
    assert any(
        "policy must allow EU deployment types" in p for p in run(tmp_path, good_main(), policy)
    )


@pytest.mark.req("NF-03")
def test_a_policy_that_only_audits_fails(tmp_path: Path) -> None:
    policy = good_policy()
    policy["resources"][0]["properties"]["policyRule"]["then"]["effect"] = "audit"
    assert any("must deny" in p for p in run(tmp_path, good_main(), policy))


@pytest.mark.req("NF-03")
def test_a_missing_deployment_type_policy_fails(tmp_path: Path) -> None:
    policy = good_policy()
    policy["resources"] = []
    assert any("no policy refuses" in p for p in run(tmp_path, good_main(), policy))


@pytest.mark.req("NF-03")
def test_a_policy_that_allows_ai_outside_the_eu_fails(tmp_path: Path) -> None:
    policy = good_policy()
    policy["parameters"]["allowedAiLocations"]["defaultValue"].append("eastus")
    assert any("EU regions only" in p for p in run(tmp_path, good_main(), policy))


@pytest.mark.req("NF-03")
def test_type_and_policy_must_allow_the_same_types(tmp_path: Path) -> None:
    policy = good_policy()
    policy["parameters"]["allowedDeploymentSkus"]["defaultValue"] = ["DataZoneStandard"]
    assert any("allow different" in p for p in run(tmp_path, good_main(), policy))


@pytest.mark.req("NF-01")
def test_the_check_reports_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main = copy.deepcopy(good_main())
    main["parameters"]["retentionLocked"]["defaultValue"] = True
    run(tmp_path, main)
    assert check_infra.main([str(tmp_path)]) == 1
    assert "Infrastructure problem" in capsys.readouterr().out


def backup_module(main: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = main["resources"]["backup"]["properties"]["template"]
    return template


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("days", [7, 34, None])
def test_database_backups_shorter_than_35_days_fail(tmp_path: Path, days: int | None) -> None:
    main = good_main()
    server = main["resources"]["database"]["properties"]["template"]["resources"]["server"]
    if days is None:
        del server["properties"]["backup"]
    else:
        server["properties"]["backup"]["backupRetentionDays"] = days
    assert any("not 35" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_no_backup_vault_fails(tmp_path: Path) -> None:
    main = good_main()
    del main["resources"]["backup"]
    assert any("no Backup vault" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_long_term_backups_without_the_years_parameter_fail(tmp_path: Path) -> None:
    main = good_main()
    del main["parameters"]["databaseBackupYears"]
    assert any("no databaseBackupYears parameter" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_long_term_backups_kept_less_than_ten_years_by_default_fail(tmp_path: Path) -> None:
    main = good_main()
    main["parameters"]["databaseBackupYears"]["defaultValue"] = 8
    assert any("default to at least 10" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
def test_long_term_backups_kept_less_than_ten_years_in_the_parameters_fail(
    tmp_path: Path,
) -> None:
    parameters = good_parameters()
    parameters["parameters"]["databaseBackupYears"]["value"] = 5
    problems = run(tmp_path, good_main(), parameters=parameters)
    assert any("databaseBackupYears in the parameters file" in p for p in problems)


@pytest.mark.req("NF-01")
def test_a_backup_policy_with_a_fixed_period_fails(tmp_path: Path) -> None:
    main = good_main()
    monthly = backup_module(main)["resources"]["policy"]["properties"]["policyRules"][2]
    monthly["lifecycles"][0]["deleteAfter"]["duration"] = "P1Y"
    assert any("period set by a parameter" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    "state",
    [
        "Locked",
        "Disabled",
        "[if(parameters('retentionLocked'), 'Locked', 'Disabled')]",
        "[if(parameters('retentionLocked'), 'Unlocked', 'Disabled')]",
    ],
)
def test_a_vault_lock_written_straight_into_the_code_fails(tmp_path: Path, state: str) -> None:
    main = good_main()
    vault = backup_module(main)["resources"]["vault"]
    vault["properties"]["securitySettings"]["immutabilitySettings"]["state"] = state
    assert any("immutability must come from retentionLocked" in p for p in run(tmp_path, main))


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("state", ["Off", "[if(parameters('retentionLocked'), 'AlwaysOn', 'Off')]"])
def test_a_vault_without_soft_delete_fails(tmp_path: Path, state: str) -> None:
    main = good_main()
    vault = backup_module(main)["resources"]["vault"]
    vault["properties"]["securitySettings"]["softDeleteSettings"]["state"] = state
    assert any("soft delete off" in p for p in run(tmp_path, main))


def standin_resources(main: dict[str, Any]) -> dict[str, Any]:
    resources: dict[str, Any] = main["resources"]["standins"]["properties"]["template"]["resources"]
    return resources


@pytest.mark.req("NF-04")
def test_demo_mode_on_by_default_in_the_template_fails(tmp_path: Path) -> None:
    main = good_main()
    main["parameters"]["demo"]["defaultValue"] = True
    assert run(tmp_path, main) == ["demo must default to false in the template"]


@pytest.mark.req("NF-04")
def test_a_stand_in_the_internet_can_reach_fails(tmp_path: Path) -> None:
    main = good_main()
    standin_resources(main)["apps"]["properties"]["configuration"]["ingress"]["external"] = True
    assert run(tmp_path, main) == ["module standins has an app that the internet can reach"]


@pytest.mark.req("NF-04")
def test_a_stand_in_without_a_word_on_ingress_fails(tmp_path: Path) -> None:
    main = good_main()
    del standin_resources(main)["apps"]["properties"]["configuration"]["ingress"]["external"]
    assert run(tmp_path, main) == []  # Azure's default for ingress is internal
    standin_resources(main)["apps"]["properties"]["configuration"]["ingress"]["external"] = (
        "[parameters('public')]"
    )
    assert run(tmp_path, main) == ["module standins has an app that the internet can reach"]


@pytest.mark.req("NF-04")
def test_stand_ins_deployed_without_demo_mode_fail(tmp_path: Path) -> None:
    main = good_main()
    main["resources"]["standins"]["condition"] = "[not(empty(parameters('demoImage')))]"
    assert run(tmp_path, main) == ["the stand ins must only be deployed in demo mode"]


@pytest.mark.req("NF-06")
def test_a_job_secret_that_is_not_read_from_key_vault_fails(tmp_path: Path) -> None:
    main = good_main()
    secret = standin_resources(main)["seedJob"]["properties"]["configuration"]["secrets"][0]
    del secret["keyVaultUrl"]
    secret["value"] = "[parameters('token')]"
    assert run(tmp_path, main) == [
        "app caj-argus-demo-seed gets a secret that is not a Key Vault reference"
    ]


@pytest.mark.req("NF-04", "NF-06")
def test_demo_mode_with_real_secrets_in_the_parameters_fails(tmp_path: Path) -> None:
    parameters = good_parameters()
    parameters["parameters"]["manualSecrets"]["value"] = [
        {"secretName": "wise-api-token", "envName": "WISE_API_TOKEN"}
    ]
    problems = run(tmp_path, good_main(), parameters=parameters)
    assert len(problems) == 1
    assert "switches demo mode on and also hands real secrets" in problems[0]
    parameters["parameters"]["demo"]["value"] = False
    assert run(tmp_path, good_main(), parameters=parameters) == []


def test_a_template_without_stand_ins_or_demo_switch_fails(tmp_path: Path) -> None:
    main = good_main()
    del main["resources"]["standins"]
    del main["parameters"]["demo"]
    assert run(tmp_path, main) == [
        "main template has no demo parameter",
        "no standins module found",
    ]


def apps_module(main: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = main["resources"]["apps"]["properties"]["template"]
    return template


def intake_job(main: dict[str, Any]) -> dict[str, Any]:
    job: dict[str, Any] = apps_module(main)["resources"][0]
    return job


def alerts_module(main: dict[str, Any]) -> dict[str, Any]:
    template: dict[str, Any] = main["resources"]["alerts"]["properties"]["template"]
    return template


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize(
    ("cron", "problem"),
    [
        ("*/15 * * * *", "the sweep job must run on the schedule '*/5 * * * *'"),
        ("0 */6 * * *", "the sweep job must run on the schedule '*/5 * * * *'"),
    ],
)
def test_a_sweep_that_does_not_run_every_5_minutes_fails(
    tmp_path: Path, cron: str, problem: str
) -> None:
    main = good_main()
    apps_module(main)["variables"]["intakeJobList"][0]["cron"] = cron
    assert run(tmp_path, main) == [problem]


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize("cron", ["*/30 * * * *", "30 2 * * 1", "30 2,14 * * *", ""])
def test_a_removal_that_does_not_run_once_a_day_fails(tmp_path: Path, cron: str) -> None:
    main = good_main()
    apps_module(main)["variables"]["intakeJobList"][1]["cron"] = cron
    assert run(tmp_path, main) == ["the removal job must run once a day"]


@pytest.mark.req("IN-01", "NF-01")
def test_a_missing_intake_job_fails(tmp_path: Path) -> None:
    main = good_main()
    del apps_module(main)["variables"]["intakeJobList"][0]
    assert run(tmp_path, main) == ["the sweep job must run on the schedule '*/5 * * * *'"]


@pytest.mark.req("IN-01", "NF-01")
def test_an_intake_job_without_its_schedule_check_fails(tmp_path: Path) -> None:
    main = good_main()
    apps_module(main)["variables"]["intakeJobList"].append({"job": "digest", "cron": "0 6 * * *"})
    problems = run(tmp_path, main)
    assert "unknown intake jobs ['digest']: add their schedule to this check" in problems


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("triggerType", "Manual"), "must run on a schedule"),
        (("replicaRetryLimit", 2), "must not retry"),
        (("replicaTimeout", None), "needs a time limit per run"),
    ],
)
def test_an_intake_job_that_retries_or_has_no_schedule_fails(
    tmp_path: Path, change: tuple[str, Any], problem: str
) -> None:
    main = good_main()
    key, value = change
    intake_job(main)["properties"]["configuration"][key] = value
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert problem in problems[0]


@pytest.mark.req("IN-01", "NF-01")
def test_two_sweeps_at_once_fail(tmp_path: Path) -> None:
    main = good_main()
    trigger = intake_job(main)["properties"]["configuration"]["scheduleTriggerConfig"]
    trigger["parallelism"] = 2
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert "one replica at a time" in problems[0]


@pytest.mark.req("IN-01")
def test_intake_jobs_that_cannot_be_switched_off_fail(tmp_path: Path) -> None:
    main = good_main()
    del intake_job(main)["condition"]
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert "only run when the intake jobs are on" in problems[0]


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("index", range(4))
def test_an_intake_job_outcome_no_alert_watches_fails(tmp_path: Path, index: int) -> None:
    main = good_main()
    rule = alerts_module(main)["variables"]["intakeRules"][index]
    mark = rule["query"].split('"')[1]
    rule["query"] = "Logs | take 1"
    assert run(tmp_path, main) == [f"no alert watches the log line {mark!r}"]


@pytest.mark.req("NF-01")
def test_an_alert_without_an_action_group_fails(tmp_path: Path) -> None:
    main = good_main()
    alerts_module(main)["resources"]["intakeAlerts"]["properties"]["actions"]["actionGroups"] = []
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert "sends to no action group" in problems[0]


@pytest.mark.req("NF-01")
def test_no_action_group_fails(tmp_path: Path) -> None:
    main = good_main()
    del alerts_module(main)["resources"]["actionGroup"]
    assert run(tmp_path, main) == ["no action group for the operations alerts"]


@pytest.mark.req("NF-03")
def test_an_alert_address_written_in_the_code_fails(tmp_path: Path) -> None:
    main = good_main()
    group = alerts_module(main)["resources"]["actionGroup"]
    group["properties"]["emailReceivers"] = [{"emailAddress": "someone@maxasp.example"}]
    assert run(tmp_path, main) == ["an alert email address is written in the code"]


@pytest.mark.req("NF-03")
def test_only_an_action_group_may_be_global(tmp_path: Path) -> None:
    main = good_main()
    assert run(tmp_path, main) == []
    rules = alerts_module(main)["resources"]["intakeAlerts"]
    rules["location"] = "global"
    assert run(tmp_path, main) == [
        "resource [format('alert-{0}', variables('intakeRules')"
        "[copyIndex()].kind)] has a fixed location 'global'"
    ]


# Sign in with Microsoft in Azure (#133, NF-03).


def test_the_check_knows_the_five_sign_in_settings() -> None:
    assert set(check_infra.SIGN_IN_SETTINGS) == set(SIGN_IN_FIELDS)


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("setting", list(SIGN_IN_FIELDS))
def test_a_sign_in_setting_missing_from_the_web_app_fails(tmp_path: Path, setting: str) -> None:
    main = good_main()
    variables = apps_variables(main)
    variables["signInEnv"] = [e for e in variables["signInEnv"] if e["name"] != setting]

    assert run(tmp_path, main) == [
        f"app ca-argus-web does not always get the sign in setting {setting}"
    ]


@pytest.mark.req("NF-03")
def test_sign_in_settings_left_out_of_the_web_app_env_fail(tmp_path: Path) -> None:
    main = good_main()
    container = web_app(main)["properties"]["template"]["containers"][0]
    container["env"] = WEB_APP_ENV.replace("variables('signInEnv'), ", "")

    problems = run(tmp_path, main)

    assert len(problems) == 5
    assert all("does not always get the sign in setting" in problem for problem in problems)


@pytest.mark.req("NF-03")
def test_sign_in_settings_only_in_demo_mode_fail(tmp_path: Path) -> None:
    main = good_main()
    container = web_app(main)["properties"]["template"]["containers"][0]
    container["env"] = WEB_APP_ENV.replace(
        "variables('signInEnv'), if(parameters('demo'), variables('demoSettings'), createArray())",
        "if(parameters('demo'), variables('signInEnv'), createArray())",
    )

    problems = run(tmp_path, main)

    assert len(problems) == 5
    assert all("does not always get the sign in setting" in problem for problem in problems)


@pytest.mark.req("NF-03")
def test_sign_in_settings_written_straight_into_the_env_list_pass(tmp_path: Path) -> None:
    main = good_main()
    web_app(main)["properties"]["template"]["containers"][0]["env"] = sign_in_env()

    assert run(tmp_path, main) == []


@pytest.mark.req("NF-03")
def test_a_sign_in_setting_with_a_fixed_value_fails(tmp_path: Path) -> None:
    main = good_main()
    apps_variables(main)["signInEnv"][3]["value"] = "11111111-2222-3333-4444-555555555555"

    assert run(tmp_path, main) == [
        "app ca-argus-web gets the sign in setting ARGUS_ENTRA_GROUP_FINANCE as a fixed value; "
        "it must come from a parameter (GitHub variables)"
    ]


@pytest.mark.req("NF-03")
def test_the_fake_sign_in_service_setting_in_azure_fails(tmp_path: Path) -> None:
    main = good_main()
    apps_variables(main)["signInEnv"].append(
        {"name": "ARGUS_ENTRA_AUTHORITY", "value": "http://localhost:9000"}
    )

    assert run(tmp_path, main) == [
        "ARGUS_ENTRA_AUTHORITY is for the local fake only and must stay unset in Azure"
    ]


def test_no_web_app_fails(tmp_path: Path) -> None:
    main = good_main()
    del main["resources"]["apps"]["properties"]["template"]["resources"][1]

    assert run(tmp_path, main) == ["no ARGUS web app found in the apps module"]


# --- The nightly audit copy job (#103) ---


def audit_copy_job(main: dict[str, Any]) -> dict[str, Any]:
    job: dict[str, Any] = apps_module(main)["resources"][2]
    return job


@pytest.mark.req("NF-01", "NF-05")
@pytest.mark.parametrize("cron", ["0 2 * * *", "*/5 * * * *", "0 1 * * 0", ""])
def test_an_audit_copy_off_its_nightly_schedule_fails(tmp_path: Path, cron: str) -> None:
    main = good_main()
    apps_module(main)["variables"]["auditJob"]["cron"] = cron
    assert run(tmp_path, main) == [
        "audit copy job [parameters('auditJobName')] must run on the schedule '0 1 * * *'"
    ]


@pytest.mark.req("NF-01")
def test_no_audit_copy_job_fails(tmp_path: Path) -> None:
    main = good_main()
    del apps_module(main)["resources"][2]
    assert run(tmp_path, main) == ["no job runs the nightly audit copy (python -m argus.audit)"]


@pytest.mark.req("NF-01")
def test_two_audit_copy_jobs_fail(tmp_path: Path) -> None:
    main = good_main()
    apps_module(main)["resources"].append(audit_job())
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert "more than one job runs the nightly audit copy" in problems[0]


@pytest.mark.req("NF-01")
@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("triggerType", "Manual"), "must run on a schedule"),
        (("replicaRetryLimit", 1), "must not retry"),
        (("replicaTimeout", None), "needs a time limit per run"),
    ],
)
def test_an_audit_copy_job_that_retries_or_has_no_schedule_fails(
    tmp_path: Path, change: tuple[str, Any], problem: str
) -> None:
    main = good_main()
    key, value = change
    audit_copy_job(main)["properties"]["configuration"][key] = value
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert problems[0].startswith("audit copy job ")
    assert problem in problems[0]


@pytest.mark.req("NF-01")
def test_two_audit_copies_at_once_fail(tmp_path: Path) -> None:
    main = good_main()
    trigger = audit_copy_job(main)["properties"]["configuration"]["scheduleTriggerConfig"]
    trigger["parallelism"] = 2
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert "one replica at a time" in problems[0]


@pytest.mark.req("NF-01")
def test_an_audit_copy_job_that_runs_with_the_placeholder_image_fails(tmp_path: Path) -> None:
    main = good_main()
    del audit_copy_job(main)["condition"]
    assert run(tmp_path, main) == [
        "audit copy job [parameters('auditJobName')] must not run with the placeholder image"
    ]


@pytest.mark.req("NF-01")
@pytest.mark.parametrize("index", range(2))
def test_an_audit_copy_outcome_no_alert_watches_fails(tmp_path: Path, index: int) -> None:
    main = good_main()
    rule = alerts_module(main)["variables"]["auditRules"][index]
    mark = rule["query"].split('"')[1]
    rule["query"] = "Logs | take 1"
    assert run(tmp_path, main) == [f"no alert watches the log line {mark!r}"]


@pytest.mark.req("NF-01")
def test_an_audit_alert_without_an_action_group_fails(tmp_path: Path) -> None:
    main = good_main()
    alerts_module(main)["resources"]["auditAlerts"]["properties"]["actions"]["actionGroups"] = []
    problems = run(tmp_path, main)
    assert len(problems) == 1
    assert "sends to no action group" in problems[0]


# --- Secrets of every app and job only as Key Vault references (#103, NF-06) ---


@pytest.mark.req("NF-06")
def test_a_secret_written_as_a_loop_is_checked_too(tmp_path: Path) -> None:
    main = good_main()
    loop = audit_copy_job(main)["properties"]["configuration"]["copy"][0]
    loop["input"] = {"name": "argus-documents", "value": "[parameters('connection')]"}
    assert run(tmp_path, main) == [
        "app [parameters('auditJobName')] gets a secret that is not a Key Vault reference"
    ]


@pytest.mark.req("NF-06")
def test_a_secret_list_built_by_an_expression_without_key_vault_fails(tmp_path: Path) -> None:
    main = good_main()
    secrets = standin_resources(main)["seedJob"]["properties"]["configuration"]
    secrets["secrets"] = "[createArray(createObject('name', 'token', 'value', parameters('t')))]"
    assert run(tmp_path, main) == [
        "app caj-argus-demo-seed gets a secret that is not a Key Vault reference"
    ]


@pytest.mark.req("NF-06")
def test_a_connection_string_as_a_plain_setting_fails(tmp_path: Path) -> None:
    main = good_main()
    apps_variables(main)["plainSettings"].append(
        {"name": "ARGUS_DOCUMENTS_CONNECTION_STRING", "value": "[parameters('connection')]"}
    )
    problems = run(tmp_path, main)
    # Both the web app and the audit copy job use plainSettings here.
    assert len(problems) == 2
    assert all(
        "gets the secret setting ARGUS_DOCUMENTS_CONNECTION_STRING as a plain value" in p
        for p in problems
    )


@pytest.mark.req("NF-06")
def test_a_key_written_straight_into_a_job_env_fails(tmp_path: Path) -> None:
    main = good_main()
    container = audit_copy_job(main)["properties"]["template"]["containers"][0]
    container["env"] = [{"name": "AZURE_OPENAI_API_KEY", "value": "[parameters('key')]"}]
    assert run(tmp_path, main) == [
        "app [parameters('auditJobName')] gets the secret setting AZURE_OPENAI_API_KEY as a "
        "plain value, not from Key Vault"
    ]


@pytest.mark.req("NF-06")
def test_secrets_handed_over_as_values_by_a_loop_fail(tmp_path: Path) -> None:
    main = good_main()
    container = audit_copy_job(main)["properties"]["template"]["containers"][0]
    container["env"] = AUDIT_JOB_ENV.replace("'secretRef'", "'value'")
    assert run(tmp_path, main) == [
        "app [parameters('auditJobName')] hands secrets over as plain settings"
    ]


@pytest.mark.req("NF-06")
def test_a_stand_in_token_as_a_secret_reference_passes(tmp_path: Path) -> None:
    main = good_main()
    container = audit_copy_job(main)["properties"]["template"]["containers"][0]
    container["env"] = [{"name": "ARGUS_GRAPH_STANDIN_TOKEN", "secretRef": "token"}]
    assert run(tmp_path, main) == []
