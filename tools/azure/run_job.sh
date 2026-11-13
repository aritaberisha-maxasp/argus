#!/usr/bin/env bash
# Starts a Container Apps job and waits until it has finished (#124). Fails when the job fails
# or takes longer than the timeout. Its output is in Log Analytics, not here.
#
# Usage: tools/azure/run_job.sh <job name> [timeout in seconds, default 600] [command ...]
#
# With a command, this one run of the job uses it instead of the job's own command (#136). The
# image, settings and secrets stay those of the job: they are read from the job and passed
# along unchanged, so only the command differs. Of the resources only cpu and memory go back,
# since the others Azure reports are read only (review of PR 140).
#
# Needs: RESOURCE_GROUP and a signed in az, plus jq when a command is given.
set -euo pipefail

job=$1
timeout=${2:-600}
if [[ ! "$timeout" =~ ^[0-9]+$ ]]; then
  echo "::error::The timeout in seconds comes before the command: run_job.sh <job> <timeout> <command ...>" >&2
  exit 2
fi
api=2024-03-01

id=$(az resource show --resource-group "$RESOURCE_GROUP" --name "$job" \
  --resource-type Microsoft.App/jobs --query id --output tsv)
start=(az rest --method post --url "https://management.azure.com$id/start?api-version=$api")
if (($# > 2)); then
  shift 2
  command=$(printf '%s\n' "$@" | jq -R . | jq -cs .)
  body=$(az resource show --ids "$id" --query properties.template.containers --output json |
    jq -c --argjson command "$command" '
      if length != 1 then error("the job must have exactly one container") else . end
      | {containers: [.[0] | {name, image, env, resources: (.resources | {cpu, memory}), command: $command}]}')
  start+=(--body "$body")
fi
execution=$("${start[@]}" --query name --output tsv)
# Azure may answer the start with 202 and no body. Then take the newest execution instead.
if [[ -z "$execution" ]]; then
  sleep 5
  execution=$(az rest --method get \
    --url "https://management.azure.com$id/executions?api-version=$api" \
    --query "sort_by(value, &properties.startTime)[-1].name" --output tsv)
fi
if [[ -z "$execution" ]]; then
  echo "::error::$job was started, but Azure named no execution to follow." >&2
  exit 1
fi
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
