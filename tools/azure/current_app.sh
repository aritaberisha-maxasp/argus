#!/usr/bin/env bash
# Prints the image and port the ARGUS app runs right now, as GitHub step outputs (#81).
#
# The pipeline applies the infrastructure code with these first, so the running version keeps
# running until the migrations have passed. On the very first deploy there is no app yet, so
# it prints the public placeholder the templates start with.
#
# Only an answer from Azure that the app does not exist counts as "first deploy". Any error
# (throttling, network, an expired sign in) stops the run, so a passing fault can never put
# the placeholder over the running app (review of PR 110).
#
# Needs: RESOURCE_GROUP, APP_NAME, PLACEHOLDER_IMAGE, PLACEHOLDER_PORT, and a signed in az.
set -euo pipefail

found=$(az resource list \
  --resource-group "$RESOURCE_GROUP" \
  --resource-type Microsoft.App/containerApps \
  --name "$APP_NAME" \
  --query "length(@)" \
  --output tsv)

case "$found" in
  0)
    image=$PLACEHOLDER_IMAGE
    port=$PLACEHOLDER_PORT
    echo "No app yet. The first deploy starts with the placeholder $image." >&2
    ;;
  1)
    current=$(az resource show \
      --resource-group "$RESOURCE_GROUP" \
      --name "$APP_NAME" \
      --resource-type Microsoft.App/containerApps \
      --query "[properties.template.containers[0].image, to_string(properties.configuration.ingress.targetPort)]" \
      --output tsv)
    image=$(sed -n 1p <<<"$current")
    port=$(sed -n 2p <<<"$current")
    if [[ -z "$image" || -z "$port" ]]; then
      echo "::error::Could not read the image and port of $APP_NAME." >&2
      exit 1
    fi
    echo "The app runs $image on port $port." >&2
    ;;
  *)
    echo "::error::Unexpected answer when looking for $APP_NAME: '$found'." >&2
    exit 1
    ;;
esac

echo "image=$image"
echo "port=$port"
