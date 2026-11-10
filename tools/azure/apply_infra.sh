#!/usr/bin/env bash
# Applies infra/main.bicep with infra/main.bicepparam to the ARGUS resource group (#81) and
# prints the deployment outputs as GitHub step outputs (name=value).
#
# Usage: tools/azure/apply_infra.sh <deployment name>
# Needs: RESOURCE_GROUP, ARGUS_APP_IMAGE, ARGUS_APP_PORT, ARGUS_DATABASE_ADMIN_OBJECT_ID,
# ARGUS_DATABASE_ADMIN_NAME, and a signed in az with Bicep. main.bicepparam reads the
# ARGUS_* values from the environment.
#
# A new app revision can fail once while fresh role assignments spread through Azure
# (Key Vault and registry access for the app identity). So a failed run is tried once more.
set -euo pipefail

name=$1
retry_after=${RETRY_AFTER_SECONDS:-90}

apply() {
  az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$1" \
    --parameters infra/main.bicepparam \
    --query properties.outputs \
    --output json
}

if ! outputs=$(apply "$name"); then
  echo "::warning::Deployment $name failed. A new app revision can fail once while role assignments spread, so it runs once more in ${retry_after} seconds."
  sleep "$retry_after"
  outputs=$(apply "$name-retry")
fi

jq -r 'to_entries[] | "\(.key)=\(.value.value)"' <<<"$outputs"
