#!/usr/bin/env bash
# Prints the image and port the ARGUS app runs right now, as GitHub step outputs (#81).
#
# The pipeline applies the infrastructure code with these first, so the running version keeps
# running until the migrations have passed. On the very first deploy there is no app yet, so
# it prints the public placeholder the templates start with.
#
# Needs: RESOURCE_GROUP, APP_NAME, PLACEHOLDER_IMAGE, PLACEHOLDER_PORT, and a signed in az.
set -euo pipefail

if current=$(az resource show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --resource-type Microsoft.App/containerApps \
  --query "[properties.template.containers[0].image, to_string(properties.configuration.ingress.targetPort)]" \
  --output tsv 2>/dev/null); then
  image=$(sed -n 1p <<<"$current")
  port=$(sed -n 2p <<<"$current")
  echo "The app runs $image on port $port." >&2
else
  image=$PLACEHOLDER_IMAGE
  port=$PLACEHOLDER_PORT
  echo "No app yet. The first deploy starts with the placeholder $image." >&2
fi

echo "image=$image"
echo "port=$port"
