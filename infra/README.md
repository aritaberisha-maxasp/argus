# ARGUS in Azure

This folder describes everything ARGUS needs in Azure as code (Bicep). It follows ADR 0001 and ADR 0006 and covers issue #65.

Arita decided on 28 Oct 2026 that deployments run from CI. The pipeline from #81 (`.github/workflows/deploy.yml`) applies `main.bicep` with `main.bicepparam` on every merge to `main`, and every PR that changes this folder shows a preview of what would change. Only the one time setup below and the retention lock switch stay Arita's steps.

## What gets created

Everything sits in ARGUS's own resource group in Germany West Central.

| Resource | File | Why |
| --- | --- | --- |
| Managed identity `id-argus-app` | `main.bicep` | The app's own identity. It reads Key Vault and pulls the image without any password. |
| Log Analytics workspace | `modules/monitoring.bicep` | Logs of the app and jobs, kept 30 days. |
| Container Registry (Basic, no admin user) | `modules/registry.bicep` | Holds the ARGUS image. The app may only pull. |
| Key Vault (Azure roles only, purge protection) | `modules/keyvault.bicep` | Every secret. The app may read secrets, nothing else. |
| Storage account for originals, geo redundant into Germany North | `modules/documents.bicep` | Original emails and attachments, unchangeable for 4018 days (NF-01). See "How long originals are kept" below. |
| PostgreSQL Flexible Server 16, smallest burstable size, 35 days of backups | `modules/database.bicep` | Case data and audit log. Entra ID sign in only, no password exists. |
| Azure OpenAI with an EU model deployment | `modules/ai.bicep` | Sorting emails (ADR 0006). |
| Azure AI Document Intelligence | `modules/ai.bicep` | Reading PDFs and scans (ADR 0006). |
| Container Apps environment and the web app | `modules/apps.bicep` | Runs ARGUS. The app signs in to the database as its own Entra role and works as `argus_app`. The scheduled jobs from ADR 0001 join this environment as each job story adds its command. |
| Azure Policy for the resource group | `policy/eu-only.bicep` | Refuses resources outside the allowed regions and any Global model deployment type. |

## No secrets in the code

- The storage connection string and the two AI keys are written straight from Azure into Key Vault by the templates. They never show up in the code, a parameter or a deployment output.
- The database has no password at all. The app signs in with a token from its managed identity, and the pipeline runs the migrations with a token of the deploy identity.
- The Microsoft Graph and Wise secrets are the only ones Arita puts into Key Vault by hand. Once a secret is in the vault, add it to `manualSecrets` in `main.bicepparam` so the app gets it, for example `{ secretName: 'wise-api-token', envName: 'WISE_API_TOKEN' }`.
- The app reads every secret as a Key Vault reference through its managed identity (`src/argus/secrets.py`).

## Only EU processing for the AI services

ADR 0006 allows Azure OpenAI only as a Data Zone (EU) deployment or a regional deployment in an EU region. Three things make sure of that:

1. The type `euDeploymentSku` in `types.bicep` only accepts `DataZoneStandard` and `Standard`, so a template with a Global type does not even compile.
2. `aiLocation` in `main.bicep` only accepts EU regions.
3. The Azure Policy in `policy/eu-only.bicep` refuses any other deployment type and any AI account outside the EU. Arita deploys it once. The pipeline has no right to change policies, so the rule holds even if someone edits the templates later.

## How CI checks it

The CI job "Infrastructure code" compiles and lints every template without signing in to Azure, then runs `tools/check_infra.py` on the result. It fails when a resource could leave Germany West Central, when the retention lock is on by default or set anywhere but through its parameter, when something looks like a secret, or when a Global deployment type could get through.

To run the same check locally you need the Bicep CLI:

```
az bicep build --file infra/main.bicep --outdir build/infra
az bicep build --file infra/policy/eu-only.bicep --outdir build/infra
az bicep build-params --file infra/main.bicepparam --outfile build/infra/main.parameters.json
python tools/check_infra.py build/infra
```

## One time setup (Arita)

These need rights the pipeline must never have. The full runbook with every command is [docs/operations/deploy.md](../docs/operations/deploy.md). In short:

1. Create the resource group `rg-argus` in Germany West Central, and `rg-argus-cicd` for the two pipeline identities.
2. Deploy the EU policy once, and assign the built in policy that keeps Key Vault on Azure roles:
   `az deployment sub create --location germanywestcentral --template-file infra/policy/eu-only.bicep --parameters resourceGroupName=rg-argus`
3. Create the app identity `id-argus-app`, then the deploy and preview identities with their GitHub trust. The deploy identity gets Contributor on `rg-argus`, plus "Role Based Access Control Administrator" limited to the two roles the templates hand out (AcrPull and Key Vault Secrets User) and to the app identity. It gets no right to read Key Vault secrets and no right to change policies. The preview identity may only read and ask for a what if.
4. After the first deploy, put a delete lock on the storage account for originals and on the database server, so nobody can delete either one while the retention lock is still off. The deploy identity cannot create or remove locks, which is why this stays Arita's step. The account and server names are in the deployment outputs `documentsAccountName` and `databaseServerName`.
   `az lock create --name argus-keep --lock-type CanNotDelete --resource-group rg-argus --resource-type Microsoft.Storage/storageAccounts --resource-name <documents account>`
   `az lock create --name argus-keep --lock-type CanNotDelete --resource-group rg-argus --resource-type Microsoft.DBforPostgreSQL/flexibleServers --resource-name <database server>`
5. After the first deploy, put the Microsoft Graph and Wise secrets into Key Vault.

## How long originals are kept

ADR 0001 keeps every original for 10 years counted from the end of the calendar year it arrives in (§ 147 (4) AO). Azure counts the lock from the day a file is written, so a plain 10 years would end almost a year too early for a file stored in January. `retentionDays` is therefore 4018 days: 10 years plus the longest possible run to the end of the year, leap days included. It cannot be set lower, and the CI check fails if it drifts below that.

Set this before the first file is stored. Raising it later only covers files written after the change, and once the lock is on the period can never go down. Cases where the period starts even later, such as an open tax assessment, stay with the tax advisor question in ADR 0001.

## Switching on the retention lock (Arita only)

The original documents are protected from the start: while the policy is unlocked, no file can be changed or deleted. But an admin could still shorten or remove the policy. Locking it takes that away for good. After locking, the period can only be made longer, never shorter, and the storage account cannot be deleted while it holds protected files.

Switch it on only when all of this is true:

- The first deploy has run. Azure only accepts a new policy in the unlocked state.
- Max ASP's tax advisor confirmed the period (ADR 0001, question 2).
- ARGUS runs in production, not in a test setup.

How: change `param retentionLocked = false` to `true` in `infra/main.bicepparam` in a PR of its own. Arita merges it, and the pipeline applies it. Agents never open this PR, and the pipeline never changes the value by itself. After that the line stays `true`; setting it back to `false` would make every later deploy fail.
