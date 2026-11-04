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
