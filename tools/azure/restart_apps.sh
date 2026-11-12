#!/usr/bin/env bash
# Restarts the running revision of each container app named on the command line (#124).
#
# The stand ins keep their data in memory, so a restart empties them: the fake mailbox starts
# empty, the fake DATEV goes back to its fixtures and the Wise stand in forgets every batch.
# The ARGUS app is restarted after its demo database was rebuilt, so it opens new connections.
#
# Usage: tools/azure/restart_apps.sh <app> [<app> ...]
# Needs: RESOURCE_GROUP and a signed in az.
set -euo pipefail

api=2024-03-01
for app in "$@"; do
  id=$(az resource show --resource-group "$RESOURCE_GROUP" --name "$app" \
    --resource-type Microsoft.App/containerApps --query id --output tsv)
  revision=$(az resource show --ids "$id" --query properties.latestRevisionName --output tsv)
  az rest --method post --output none \
    --url "https://management.azure.com$id/revisions/$revision/restart?api-version=$api"
  echo "Restarted $app ($revision)."
done
