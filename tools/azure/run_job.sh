#!/usr/bin/env bash
# Starts a Container Apps job and waits until it has finished (#124). Fails when the job fails
# or takes longer than the timeout. Its output is in Log Analytics, not here.
#
# Usage: tools/azure/run_job.sh <job name> [timeout in seconds, default 600]
# Needs: RESOURCE_GROUP and a signed in az.
set -euo pipefail

job=$1
timeout=${2:-600}
api=2024-03-01

id=$(az resource show --resource-group "$RESOURCE_GROUP" --name "$job" \
  --resource-type Microsoft.App/jobs --query id --output tsv)
execution=$(az rest --method post --url "https://management.azure.com$id/start?api-version=$api" \
  --query name --output tsv)
echo "Started $job, execution $execution."

deadline=$((SECONDS + timeout))
while true; do
  status=$(az rest --method get \
    --url "https://management.azure.com$id/executions/$execution?api-version=$api" \
    --query properties.status --output tsv)
  case "$status" in
    Succeeded)
      echo "$job finished."
      exit 0
      ;;
    Failed | Stopped | Degraded)
      echo "::error::$job ended as $status. Its log is in Log Analytics (ContainerAppConsoleLogs_CL)." >&2
      exit 1
      ;;
  esac
  if ((SECONDS > deadline)); then
    echo "::error::$job did not finish within $timeout seconds (last status $status)." >&2
    exit 1
  fi
  sleep 10
done
