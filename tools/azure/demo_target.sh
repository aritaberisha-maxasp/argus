#!/usr/bin/env bash
# Prints what the Demo workflow works on, read from Azure as it runs now (#124, ADR 0009), as
# GitHub step outputs (name=value):
#
#   demo               the ARGUS_DEMO setting of the running app, true or false
#   database_server    the PostgreSQL server, from the app's ARGUS_DATABASE_URL
#   database_host      its host name
#   database_name      the database the app uses, the demo one in demo mode
#   documents_account  the demo document store (the storage account tagged role=demo)
#   standins           the stand in apps (tagged role=demo-standin), separated by spaces
#
# Reading it from the running app, not from the files in the repo, means the workflow acts on
# what is live. python -m argus.demo then checks once more that every target is demo data.
#
# Needs: RESOURCE_GROUP, APP_NAME and a signed in az.
set -euo pipefail

settings=$(az resource show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --resource-type Microsoft.App/containerApps \
  --query "properties.template.containers[0].env[?name=='ARGUS_DEMO' || name=='ARGUS_DATABASE_URL'].[name, value]" \
  --output tsv)

demo=$(awk -F'\t' '$1 == "ARGUS_DEMO" {print $2}' <<<"$settings")
url=$(awk -F'\t' '$1 == "ARGUS_DATABASE_URL" {print $2}' <<<"$settings")
if [[ -z "$url" ]]; then
  echo "::error::$APP_NAME has no ARGUS_DATABASE_URL. Has the deploy run since demo mode was switched on?" >&2
  exit 1
fi
# postgresql+psycopg://<login>@<host>:5432/<database>?sslmode=require
host=$(sed -E 's|^[^@]*@([^:/]+).*$|\1|' <<<"$url")
database=$(sed -E 's|^[^@]*@[^/]+/([^?]+).*$|\1|' <<<"$url")

# az refuses --tag together with --resource-group, so the tag is filtered in the query.
account=$(az resource list \
  --resource-group "$RESOURCE_GROUP" \
  --resource-type Microsoft.Storage/storageAccounts \
  --query "[?tags.role=='demo'].name | [0]" \
  --output tsv)

standins=$(az resource list \
  --resource-group "$RESOURCE_GROUP" \
  --resource-type Microsoft.App/containerApps \
  --query "[?tags.role=='demo-standin'].name" \
  --output tsv | tr '\n' ' ')

echo "demo=${demo:-false}"
echo "database_server=${host%%.*}"
echo "database_host=$host"
echo "database_name=$database"
echo "documents_account=$account"
echo "standins=${standins% }"
