# Setting up sign in with Microsoft

How ARGUS learns who is signed in and what they may see ([ADR 0008](../adr/0008-signing-in-to-argus.md), #23). This is a one time setup in Max ASP's Microsoft Entra ID. Someone with admin rights in the tenant does it. None of the values below is a secret.

## 1. Register the ARGUS API

In the Entra admin center, App registrations, New registration:

- Name: `ARGUS`, single tenant (only Max ASP's accounts).
- Expose an API: set the Application ID URI and add the delegated scope `access_as_user` ("Use ARGUS as the signed in person"), admins and users may consent.
- Manifest: set `accessTokenAcceptedVersion` to `2`. ARGUS only accepts v2 tokens.
- Token configuration: add the groups claim, **Groups assigned to the application**, as Group ID, for access tokens. This keeps the token small; with all groups, someone in many groups would get no role at all.
- No client secret and no certificate. ARGUS only checks tokens and never asks Entra for one with this registration.

- Authentication: add the platform **Single-page application** with the redirect address `https://<argus address>/queue`. The exception queue page (#126) signs people in through it, with PKCE and without a secret. The approval page (#38) adds its own address here when it is built.

## 2. Create three groups and assign them

Create three security groups, for example `ARGUS approvers`, `ARGUS finance` and `ARGUS admins`. Add the approvers, Benjamin and Arita. Then, under Enterprise applications, ARGUS, Users and groups, assign all three groups to the app. Only assigned groups appear in tokens. Under Properties, set **Assignment required** to Yes, so people outside the three groups cannot get a token for ARGUS at all.

A person may be in more than one group. Someone in none of them can sign in but sees nothing.

## 3. Give ARGUS the IDs

ARGUS reads these environment variables. In Azure they are plain app settings on the Container App (not Key Vault, nothing here is secret):

| Variable | Value |
| --- | --- |
| `ARGUS_ENTRA_TENANT_ID` | Directory (tenant) ID of Max ASP |
| `ARGUS_ENTRA_API_CLIENT_ID` | Application (client) ID of the ARGUS registration |
| `ARGUS_ENTRA_GROUP_APPROVER` | Object ID of the approvers group |
| `ARGUS_ENTRA_GROUP_FINANCE` | Object ID of the finance group |
| `ARGUS_ENTRA_GROUP_ADMIN` | Object ID of the admins group |

Until all five are set, ARGUS answers 503 on every route with data and logs which ones are missing. The health checks keep working.

`ARGUS_ENTRA_AUTHORITY` is only for the local fake (`standins/entra`) and must stay unset in Azure. ARGUS refuses any value other than Microsoft's sign in address or `localhost`.

## Check it

Open `https://<argus address>/queue` as Benjamin or Arita: it asks for the Microsoft sign in and then lists the open exceptions. Or ask Entra for a token with the Azure CLI and call `/me`. For the CLI to get a token, add it under Expose an API as an authorized client application (Microsoft's Azure CLI app ID `04b07795-8ddb-461a-bbee-02f9e1bf7b46`); remove it again afterwards if you like.

```bash
TOKEN=$(az account get-access-token --scope "api://<application id>/access_as_user" --query accessToken -o tsv)
curl -s https://<argus address>/me -H "Authorization: Bearer $TOKEN"
```

It answers with your object ID and roles. 401 means the token was refused (the app log says why, never the token), 403 means you are in none of the three groups.
