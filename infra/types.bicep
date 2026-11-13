// Shared types for the infrastructure code.

// The only deployment types ARGUS may use for Azure OpenAI (ADR 0006, NF-03).
// DataZoneStandard keeps processing inside the EU data zone when the account sits in an EU
// region, Standard keeps it in the account's own region. Global types can process prompts in
// any region and are refused here and by the Azure Policy in infra/policy/.
@export()
type euDeploymentSku = 'DataZoneStandard' | 'Standard'

@export()
type modelDeployment = {
  @description('Deployment name the app is configured with.')
  name: string

  @description('Model name, for example gpt-4.1-mini.')
  model: string

  @description('Pinned model version. ADR 0006: every model change reruns the fixture set first.')
  version: string

  sku: euDeploymentSku

  @description('Capacity in thousands of tokens per minute.')
  @minValue(1)
  capacity: int
}

// A Key Vault secret Arita puts in by hand, handed to the app as an environment variable.
@export()
type manualSecret = {
  @description('Name of the secret in Key Vault, letters, digits and hyphens.')
  secretName: string

  @description('Environment variable the app reads it from, for example WISE_API_TOKEN.')
  envName: string
}

// Who Max ASP is, for the sorting and the invoice reading (#125). Plain values, no secrets. The
// pipeline passes them from GitHub variables; in demo mode apps.bicep uses the synthetic values
// of the test fixtures instead.
@export()
type intakeSettingValues = {
  @description('Max ASP\'s own email domain (ARGUS_OWN_DOMAIN).')
  ownDomain: string

  @description('The address the invoicing agent sends outgoing invoices from (ARGUS_INVOICING_AGENT).')
  invoicingAgent: string

  @description('Max ASP\'s own VAT IDs, comma separated (ARGUS_OWN_VAT_IDS).')
  ownVatIds: string

  @description('Email domains the vendors in DATEV send from, comma separated (ARGUS_VENDOR_DOMAINS).')
  vendorDomains: string

  @description('Email domains the customers in DATEV send from, comma separated (ARGUS_CUSTOMER_DOMAINS).')
  customerDomains: string
}

// Sign in with Microsoft (#23, ADR 0008, #133). The IDs of the ARGUS app registration and its
// three groups in Max ASP's Entra ID. None of them is secret. The pipeline passes them from
// GitHub variables (docs/operations/deploy.md, step 7); apps.bicep hands them to the web app.
@export()
type signInSettingValues = {
  @description('Directory (tenant) ID of Max ASP (ARGUS_ENTRA_TENANT_ID).')
  tenantId: string

  @description('Application (client) ID of the ARGUS app registration (ARGUS_ENTRA_API_CLIENT_ID).')
  apiClientId: string

  @description('Object ID of the approvers group (ARGUS_ENTRA_GROUP_APPROVER).')
  approverGroupId: string

  @description('Object ID of the finance group (ARGUS_ENTRA_GROUP_FINANCE).')
  financeGroupId: string

  @description('Object ID of the admins group (ARGUS_ENTRA_GROUP_ADMIN).')
  adminGroupId: string
}
