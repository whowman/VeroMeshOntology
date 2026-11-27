#!/usr/bin/env bash
# Register the VeroMesh bot for Teams, in the customer's own Microsoft tenant, and build the
# Teams app package. This touches only Microsoft Entra ID and one Azure Bot registration
# (free tier); it does not depend on where VeroMesh runs (Azure, GCP, AWS, on premises).
#
#   HOST=ontology.example.com RESOURCE_GROUP=rg-veromesh deploy/teams/register-bot.sh
#
# Needs: the Azure CLI signed in (az login) to the customer's tenant, a subscription to hold
# the bot registration, and rights to create an app registration. It asks before creating
# anything. Prints what to give the VeroMesh install (Helm values) and writes
# veromesh-teams.zip for a Teams administrator to upload.
set -euo pipefail

HOST=${HOST:?Set HOST: where people reach VeroMesh, e.g. ontology.example.com}
RESOURCE_GROUP=${RESOURCE_GROUP:?Set RESOURCE_GROUP: the resource group for the bot registration}
BOT_NAME=${BOT_NAME:-veromesh-bot-$(echo "$HOST" | tr '.' '-' | cut -c1-30)}
APP_NAME=${APP_NAME:-VeroMesh bot ($HOST)}
OUT=${OUT:-veromesh-teams.zip}
HERE=$(cd "$(dirname "$0")" && pwd)

TENANT=$(az account show --query tenantId -o tsv)
SUB=$(az account show --query name -o tsv)
ENDPOINT="https://$HOST/api/v1/teams/messages"

cat <<MSG
This will create, in tenant $TENANT (subscription "$SUB"):
  - an Entra app registration "$APP_NAME" (this tenant only) with a client secret;
  - an Azure Bot "$BOT_NAME" in resource group $RESOURCE_GROUP, tier F0 (free),
    single-tenant, messaging endpoint $ENDPOINT, with the Microsoft Teams channel.
It changes nothing else.
MSG
read -r -p "Go ahead? [y/N] " yes
[ "$yes" = "y" ] || { echo "Nothing created."; exit 1; }

APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)
if [ -z "$APP_ID" ]; then
  APP_ID=$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  az ad sp create --id "$APP_ID" >/dev/null
fi
SECRET=$(az ad app credential reset --id "$APP_ID" --display-name veromesh-bot --years 1 --append --query password -o tsv)

if ! az bot show -g "$RESOURCE_GROUP" -n "$BOT_NAME" >/dev/null 2>&1; then
  az bot create -g "$RESOURCE_GROUP" -n "$BOT_NAME" --app-type SingleTenant --appid "$APP_ID" \
    --tenant-id "$TENANT" --endpoint "$ENDPOINT" --sku F0 >/dev/null
else
  az bot update -g "$RESOURCE_GROUP" -n "$BOT_NAME" --endpoint "$ENDPOINT" >/dev/null
fi
az bot msteams create -g "$RESOURCE_GROUP" -n "$BOT_NAME" >/dev/null 2>&1 || true

work=$(mktemp -d)
BOT_APP_ID=$APP_ID HOST=$HOST envsubst '${BOT_APP_ID} ${HOST}' < "$HERE/manifest.json" > "$work/manifest.json"
cp "$HERE/color.png" "$HERE/outline.png" "$work/"
(cd "$work" && zip -q -X "$OLDPWD/$OUT" manifest.json color.png outline.png)
rm -rf "$work"

if [ -n "${RESULT_FILE:-}" ]; then  # setup.sh teams reads these (the secret never reaches the screen)
  umask 077
  printf 'APP_ID=%s\nTENANT=%s\nSECRET=%s\n' "$APP_ID" "$TENANT" "$SECRET" > "$RESULT_FILE"
  echo "Registered; $OUT written for the Teams administrator."
  exit 0
fi

cat <<MSG

Done. Give the VeroMesh install these (Helm):
  api.teams.appId=$APP_ID
  api.teams.tenantId=$TENANT
  and the client secret in a Kubernetes Secret, key app-password:
    kubectl -n <namespace> create secret generic veromesh-teams --from-literal=app-password='<the secret below>'
    api.teams.existingSecret=veromesh-teams
Client secret (shown once; it expires in a year): $SECRET

Then a Teams administrator uploads $OUT (Teams admin center > Manage apps > Upload) and
installs it for owners and stewards with an app setup policy (deploy/teams/README.md).
Finally, on the install's Administration > Notices page, choose "Teams, or email".
MSG
