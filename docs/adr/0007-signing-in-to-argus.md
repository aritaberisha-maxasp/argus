# 0007: Signing in to ARGUS

Status: proposed
Requirements: NF-03, AP-04, AP-06, AP-08

## Context

ADR 0001 decided that approvers, Benjamin and Arita sign in with the Microsoft 365 accounts they already have, and that roles come from Entra groups. This ADR decides how the web app checks a sign in, so that an approver sees only their own invoices (#23).

ARGUS runs as an Azure Container App that is reachable from the internet (ADR 0001). Locally and in CI there is no Microsoft tenant, so the sign in must work against a stand in there, and the tests must prove that a request without a valid sign in gets no data.

## Options

1. **Container Apps built in sign in ("Easy Auth").** Azure checks the sign in before a request reaches ARGUS and passes the person on in a header. Pro: no sign in code in ARGUS. Con: ARGUS trusts a header, so anything that reaches the app another way, or a setting turned off by mistake, opens everything. It also cannot run locally or in CI, so the tests would check something other than what runs in Azure.
2. **ARGUS checks Entra access tokens itself.** The page people use gets an access token for the ARGUS API from Entra (sign in with PKCE, no client secret) and sends it with each request. ARGUS checks the signature against the keys Entra publishes, then issuer, audience, expiry, tenant and scope. Pro: the same check runs in Azure, locally and in CI; a missing setting closes the app instead of opening it. Con: a little code to own, and the key lookup must be kept up to date with Entra's rules.
3. **Separate ARGUS accounts with passwords.** Ruled out by ADR 0001.

## Decision

Option 2.

- ARGUS accepts only Entra v2 access tokens signed with RS256 by the tenant's published keys, for the ARGUS API (audience) and with the delegated scope `access_as_user`. App only tokens have no scope and are refused.
- Roles come from the `groups` claim: one Entra security group each for **approver**, **finance** (Benjamin) and **admin** (Arita). Each role needs its own group. Someone in none of them signs in but gets no data.
- Finance and admin see every case. An approver sees a case only when they are its approver or the deputy standing in (AP-06). A case they may not see answers exactly like one that does not exist.
- The Entra object ID is what the audit log records for a person (`user:<object id>`, ADR 0005), because names and email addresses change.
- Only `/health` and `/health/database` are open. A test walks every route of the app to keep it that way. The generated API pages are turned off.
- Without the sign in settings every route with data answers 503.
- Locally and in CI a fake identity provider (`standins/entra`) signs tokens with the same claims. ARGUS accepts it only on `localhost`, so it can never be trusted in Azure.

## Consequences

- The approval page (#38) signs people in with Microsoft's sign in library for single page apps and sends the token. ARGUS never sees a password.
- When someone is in more than about 200 groups, Entra leaves the groups out of the token. ARGUS then gives no role. The app registration therefore sends only the groups assigned to the app ([sign in setup](../operations/sign-in.md)).
- Who belongs to which role is managed in Entra by Max ASP's admin, not in ARGUS. Removing someone from a group takes effect when their token expires, at most about an hour later.
- The approver table (#34) and approval (#38) must provide who decides on a case. Until then an approver sees no case, which is the safe side.
