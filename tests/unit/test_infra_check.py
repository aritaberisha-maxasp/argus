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
        "resources": [
            {
                "type": "Microsoft.App/containerApps",
                "name": "ca-argus-web",
                "location": "[parameters('location')]",
                "properties": {
                    "configuration": {
                        "secrets": [
                            {
                                "name": "azure-openai-api-key",
                                "keyVaultUrl": "[format('{0}secrets/x', parameters('kv'))]",
                                "identity": "[parameters('identityId')]",
                            }
                        ]
                    }
                },
            }
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
