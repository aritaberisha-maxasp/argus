# Deploying ARGUS to Azure

ARGUS goes to Azure only through the pipeline in [.github/workflows/deploy.yml](../../.github/workflows/deploy.yml) (issue #81). Nobody deploys by hand. Arita's merge to `main` is the approval for a deploy.

This runbook has three parts: what the pipeline does, the one time setup Arita does by hand, and what to do when a deploy fails.

## What the pipeline does

**On every PR that changes `infra/`**, the workflow "Infrastructure preview" asks Azure what the PR would change (a what if) and shows the answer in the run's summary. It changes nothing. It signs in as a separate preview identity that can only read.

**After every merge to `main`**, once CI has passed on `main`, the workflow "Deploy" runs these steps in order:

0. Checks that the commit is still the tip of `main`. A re-run of an old CI run, or two CI runs that finish out of order, are skipped with a notice, so old code or an old Azure setup never goes live again.
1. Builds the app image from the [Dockerfile](../../Dockerfile), stamped with the commit.
2. Signs in to Azure with OpenID Connect. GitHub hands the run a short lived token that Azure trusts for runs on `main` only. No Azure password, client secret or key is stored in GitHub.
3. Applies `infra/main.bicep` with `infra/main.bicepparam`. The app keeps the image it runs right now.
4. Pushes the new image to ARGUS's own Container Registry.
5. Opens the database firewall for the runner's address, runs the migrations (`tools/deploy.py migrate`, which runs `alembic upgrade head`) as the database admin with an Entra token, gives the app identity its database role, makes sure the database is not owned by `azure_pg_admin` (the long term backup would skip it, #82), and closes the firewall again. Leftover rules from a run that broke off are removed first, and a rule that cannot be removed fails the run before the new image goes out.
6. Deploys the new image.
7. Smoke test: waits until `/health` shows the new commit and `/health/database` shows that the app works as `argus_app` and the database is on the newest migration. If not, the run fails.

If any step fails, the steps after it do not run. So when the migrations fail, the new image is never deployed and the running version keeps running.

When a migration fails, the log shows the error code, the table and constraint names and the last good migration, never the database's own error text. PostgreSQL puts the failing row into that text, and the GitHub log is stored outside the EU (NF-03).

A redeploy of `main` is possible by hand: Actions, Deploy, Run workflow, branch `main`. Runs on any other branch are refused, both by the workflow and by Azure.

Until Azure is set up, the deploy and the preview skip themselves with a notice and stay green. As soon as some of the GitHub variables below are set but not all, the runs fail and name what is missing.

## What the pipeline can and cannot do

| | Deploy identity | Preview identity |
| --- | --- | --- |
| Change resources in the ARGUS resource group | Yes | No, read only |
| Change anything outside that group | No | No |
| Change its own trust or rights | No, it lives in another resource group | No |
| Add a sign in to the app identity | No, a Deny policy blocks federated credentials in `rg-argus` | No |
| Read Key Vault secrets | No | No |
| Hand out roles | Only AcrPull and Key Vault Secrets User, only to the app identity | No |
| Change Azure Policy or locks | No | No |
| Switch the retention lock | No, only Arita's PR to `main.bicepparam` (infra/README.md) | No |
| Delete database backups early | No once the vault is locked. Before that, only by first switching the vault's immutability off in a merged template change | No |
| Reach Wise | No, the pipeline never calls Wise (NF-04) | No |
| Sign in to the database | Yes, as its admin, to run the migrations | No |

The CI job "Deploy pipeline" checks these promises in the workflow files on every PR with `tools/check_pipeline.py`, and rehearses the whole deploy against a local PostgreSQL.

Two limits to know about:

- Whoever can deploy code can run code as the app identity, and the app identity reads the app's secrets. A revert of the merge takes that away again, because no new sign in for the app identity can be added (step 2). That is true for every deploy pipeline. The guard is that only `main` deploys and only Arita merges to `main`. Even with the Wise token, nobody can release a payment: Wise refuses funding for that token and approval rules stop any payment by the ARGUS user (ADR 0004).
- As Contributor, the deploy identity can list the storage account and AI keys. The templates need that to write them into Key Vault. Moving the app to its managed identity for storage and AI, so no keys exist at all, is a possible follow up.

## One time setup (Arita)

Do these once, in this order. Replace the values in angle brackets. Each command runs in Azure Cloud Shell or a terminal signed in with `az login` as someone with Owner on the subscription or the resource groups.

### 1. Resource groups

```
az group create --name rg-argus --location germanywestcentral
az group create --name rg-argus-cicd --location germanywestcentral
```

`rg-argus` holds ARGUS. `rg-argus-cicd` holds only the two pipeline identities. They sit apart so the pipeline can never change its own trust.

### 2. Policies on `rg-argus`

The EU only policy from #65:

```
az deployment sub create --location germanywestcentral \
  --template-file infra/policy/eu-only.bicep --parameters resourceGroupName=rg-argus
```

Key Vault stays on Azure roles. Without this, a Contributor could switch the vault back to access policies and give itself read access to the secrets. This assigns Azure's built in policy "Azure Key Vault should use RBAC permission model" with the effect Deny:

```
az policy assignment create --name argus-keyvault-rbac-only \
  --policy 12d4fa5e-1f9f-4c21-97a9-b99b3c6611b5 \
  --scope /subscriptions/<subscription id>/resourceGroups/rg-argus \
  --params '{"effect": {"value": "Deny"}}'
```

Nobody may add a sign in (a federated credential) to an identity in `rg-argus`. Otherwise one merged workflow change could give something outside this repository a lasting way to act as the app identity and read its secrets, and that would survive a revert. The pipeline identities sit in `rg-argus-cicd`, so this does not block step 4.

```
az policy definition create --name argus-no-federated-credentials \
  --display-name "ARGUS: no federated credentials on identities" \
  --description "Refuses new sign ins for managed identities in the ARGUS resource group (#81)." \
  --mode All \
  --rules '{"if": {"field": "type", "equals": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"}, "then": {"effect": "deny"}}'

az policy assignment create --name argus-no-federated-credentials \
  --policy argus-no-federated-credentials \
  --scope /subscriptions/<subscription id>/resourceGroups/rg-argus
```

### 3. The app identity

The templates would create it, but making it first lets the role rule in step 5 name it. The templates then keep it as it is.

```
az identity create --resource-group rg-argus --name id-argus-app --location germanywestcentral
az identity show --resource-group rg-argus --name id-argus-app --query principalId --output tsv
```

Note the `principalId` as `<app principal id>`.

### 4. The two pipeline identities and their trust in GitHub

```
az identity create --resource-group rg-argus-cicd --name id-argus-deploy --location germanywestcentral
az identity create --resource-group rg-argus-cicd --name id-argus-preview --location germanywestcentral

az identity federated-credential create --resource-group rg-argus-cicd \
  --identity-name id-argus-deploy --name github-main \
  --issuer https://token.actions.githubusercontent.com \
  --subject repo:aritaberisha-maxasp/argus:ref:refs/heads/main \
  --audiences api://AzureADTokenExchange

az identity federated-credential create --resource-group rg-argus-cicd \
  --identity-name id-argus-preview --name github-pull-requests \
  --issuer https://token.actions.githubusercontent.com \
  --subject repo:aritaberisha-maxasp/argus:pull_request \
  --audiences api://AzureADTokenExchange
```

The deploy identity trusts runs on `main` only. A PR can never get its token, even if the PR changes the workflow file.

Note for each identity its `clientId` and `principalId`:

```
az identity show --resource-group rg-argus-cicd --name id-argus-deploy --query "{clientId: clientId, principalId: principalId}"
az identity show --resource-group rg-argus-cicd --name id-argus-preview --query "{clientId: clientId, principalId: principalId}"
```

### 5. Rights of the deploy identity

Contributor on `rg-argus` only:

```
az role assignment create --assignee-object-id <deploy principal id> \
  --assignee-principal-type ServicePrincipal --role Contributor \
  --scope /subscriptions/<subscription id>/resourceGroups/rg-argus
```

The templates give the app identity two roles: AcrPull on the registry and Key Vault Secrets User on the vault. So the deploy identity also needs "Role Based Access Control Administrator" on `rg-argus`, limited to exactly those two roles and to the app identity. Save this as `condition.txt`, with `<app principal id>` filled in:

```
(
 (
  !(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})
 )
 OR
 (
  @Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d, 4633458b-17de-408a-b874-0445c86b69e6}
  AND
  @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {<app principal id>}
 )
)
AND
(
 (
  !(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})
 )
 OR
 (
  @Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {7f951dda-4ed3-4680-a7ca-43fe172d538d, 4633458b-17de-408a-b874-0445c86b69e6}
  AND
  @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {<app principal id>}
 )
)
```

```
az role assignment create --assignee-object-id <deploy principal id> \
  --assignee-principal-type ServicePrincipal \
  --role "Role Based Access Control Administrator" \
  --scope /subscriptions/<subscription id>/resourceGroups/rg-argus \
  --condition "$(cat condition.txt)" --condition-version 2.0
```

The deploy identity gets no Key Vault data role, no right to change policies and no right to create or remove locks.

### 6. Rights of the preview identity

Reader on `rg-argus`, plus a small custom role that allows only a what if:

```
az role assignment create --assignee-object-id <preview principal id> \
  --assignee-principal-type ServicePrincipal --role Reader \
  --scope /subscriptions/<subscription id>/resourceGroups/rg-argus

az role definition create --role-definition '{
  "Name": "ARGUS deployment preview",
  "Description": "Ask Azure what a deployment would change, without changing anything.",
  "Actions": [
    "Microsoft.Resources/deployments/whatIf/action",
    "Microsoft.Resources/deployments/validate/action"
  ],
  "AssignableScopes": ["/subscriptions/<subscription id>/resourceGroups/rg-argus"]
}'

az role assignment create --assignee-object-id <preview principal id> \
  --assignee-principal-type ServicePrincipal --role "ARGUS deployment preview" \
  --scope /subscriptions/<subscription id>/resourceGroups/rg-argus
```

### 7. GitHub variables

In the repository: Settings, Secrets and variables, Actions, tab **Variables** (not Secrets, none of these is secret). Add:

| Variable | Value |
| --- | --- |
| `AZURE_TENANT_ID` | Max ASP's Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | The subscription that holds `rg-argus` |
| `AZURE_CLIENT_ID` | `clientId` of `id-argus-deploy` |
| `AZURE_PREVIEW_CLIENT_ID` | `clientId` of `id-argus-preview` |
| `ARGUS_RESOURCE_GROUP` | `rg-argus` |
| `ARGUS_DATABASE_ADMIN_OBJECT_ID` | `principalId` of `id-argus-deploy` |
| `ARGUS_DATABASE_ADMIN_NAME` | `id-argus-deploy` |

The deploy identity is also the database admin, because it runs the migrations. The app never uses it: the app signs in with its own identity and works as `argus_app`.

### 8. First deploy

Actions, Deploy, Run workflow, branch `main`. The first run takes longer, about 20 to 30 minutes, mostly for the database server. It starts the app with a public placeholder image and then replaces it with ARGUS once the migrations have run.

A new app version can fail once right after its roles are handed out, while Azure spreads them. The pipeline then waits 90 seconds and tries once more on its own.

### 9. After the first deploy

These are the steps 4 and 5 from [infra/README.md](../../infra/README.md#one-time-setup-arita):

- Delete locks on the storage account for originals, the database server and the backup vault.
- The Microsoft Graph and Wise secrets into Key Vault, then a PR that adds them to `manualSecrets` in `main.bicepparam`.
- The two roles for the backup vault, then a PR that switches `databaseBackupProtected` on, so the long term backup of the database starts ([backup-and-restore.md](backup-and-restore.md)).

The retention lock stays off until the tax advisor confirms the period. Switching it on is a separate PR by Arita (infra/README.md).

## When a deploy fails

Open the failed run under Actions, Deploy. The step that failed says what happened.

- **"Check the GitHub variables" failed.** A variable from step 7 is missing or not a real ID. The error names it.
- **"Sign in to Azure" failed.** The federated credential in step 4 does not match. Check the subject is exactly `repo:aritaberisha-maxasp/argus:ref:refs/heads/main` and the run was on `main`.
- **"Apply the infrastructure code" failed.** The error text comes from Azure. A "RoleAssignmentUpdateNotPermitted" or "AuthorizationFailed" usually means step 5 is incomplete. The running version is not changed by a failed infrastructure step except for the parts Azure already applied.
- **"Check the GitHub variables and that this is the tip of main" says nothing was deployed.** A newer commit is on `main`, and its own run deploys it. Nothing to do.
- **"Run the database migrations" failed.** The new version was not deployed and the old one keeps running. The log names the error code, the table and the constraint. To read the full error, run the migration against a copy of the database inside Azure, never in GitHub. Fix the migration in a new PR. A migration that stopped halfway is rolled back by PostgreSQL, since each migration runs in its own transaction.
- **"Close the database to this runner again" failed.** The new version was not deployed. The next deploy removes the leftover rule on its own. To remove it at once: `az postgres flexible-server firewall-rule delete --resource-group rg-argus --name <server> --rule-name <rule> --yes`.
- **"Deploy the new version" failed twice.** Look at the app's revisions in the portal (Container App, Revisions and replicas) and its log in Log Analytics.
- **"Smoke test" failed.** The log lists what each try saw. "still shows build" means the new version never started. "works as" a role other than `argus_app` means the app's database settings are wrong. "behind" means the database is not on the newest migration.

To go back to the previous version, revert the PR that broke it and merge the revert. The pipeline deploys `main` again. Migrations only go forward in Azure, so a migration has to keep working with the version before it until the next one is out. The same holds for app settings in the templates: step 3 applies new settings while the old image still runs, so a renamed setting must keep working with the version before it.
