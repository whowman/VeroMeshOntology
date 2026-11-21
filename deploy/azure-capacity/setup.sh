#!/usr/bin/env bash
# One-time Azure setup for the capacity runs on the reference machines, run in Azure
# Cloud Shell (Bash) by someone who can create a resource group and assign roles in it
# (a subscription Owner). Every step checks what exists first, so running it again is
# safe. Nothing here costs money: machines are created by the workflow, per run, and
# deleted when the run ends.
#
#   ./setup.sh            everything below, then prints what to set in GitHub
#   ./setup.sh quota      only the vCPU quota check
#   ./setup.sh raise      ask Azure for the Esv6 quota the 500,000 run needs
#   ./setup.sh show       only print what to set in GitHub
#
# What it creates, in resource group veromesh-capacity:
#   a virtual network and subnet with a network security group that allows no inbound
#   traffic (the machines reach the internet; nothing reaches them) . a managed identity
#   for GitHub Actions (OIDC: no stored password), allowed to act only in this resource
#   group, and only for this repository's `capacity` environment
#
# Override any of these by setting it before the command, e.g.
#   LOCATION=eastus ./setup.sh
set -euo pipefail

SUBSCRIPTION=${SUBSCRIPTION:-$(az account show --query id -o tsv)}
RESOURCE_GROUP=${RESOURCE_GROUP:-veromesh-capacity}
LOCATION=${LOCATION:-westus2}
GITHUB_REPO=${GITHUB_REPO:-whowman/VeroMeshOntology}
GITHUB_ENVIRONMENT=${GITHUB_ENVIRONMENT:-capacity}
IDENTITY=id-veromesh-capacity
VNET=vnet-veromesh-capacity
SUBNET=snet-capacity
NSG=nsg-veromesh-capacity

# vCPUs the runs need per family. v6, not v5: this subscription has no v5 quota in
# westus2 and Azure refuses to grant it. One machine runs at a time (the workflow's
# concurrency group), so each family needs its largest machine: D8s v6 = 8, E16s v6 = 16
# (the 500,000 run).
NEED_D=8
NEED_E=16
FAMILY_D="Standard Dsv6 Family vCPUs"
FAMILY_E="Standard Esv6 Family vCPUs"

say() { printf '\n== %s\n' "$*"; }

quota() {
  say "vCPU quota in $LOCATION (needed: Dsv6 $NEED_D, Esv6 $NEED_E, all regional $((NEED_E + 4)))"
  local short=0
  # Match on the display name, ignoring case: Azure spells the families' internal names
  # inconsistently (standardDSv5Family, standardDsv6Family, ...).
  while IFS=$'\t' read -r name current limit; do
    need=-1
    shopt -s nocasematch
    case "$name" in
      "$FAMILY_D") need=$NEED_D ;;
      "$FAMILY_E") need=$NEED_E ;;
      "Total Regional vCPUs") need=$((NEED_E + 4)) ;;  # the largest machine, beside a 4-vCPU AKS
    esac
    shopt -u nocasematch
    [ "$need" -lt 0 ] && continue
    free=$((limit - current))
    status="ok"
    if [ "$free" -lt "$need" ]; then status="TOO LOW: ask for $((current + need))"; short=1; fi
    printf '  %-28s in use %4s of %4s   %s\n' "$name" "$current" "$limit" "$status"
  done < <(az vm list-usage -l "$LOCATION" --query "[].[localName, currentValue, limit]" -o tsv)
  if [ "$short" = 1 ]; then
    echo "  Raise it in the portal (Quotas > Compute, region $LOCATION; set the usage filter to show all),"
    echo "  or: ./setup.sh raise. Free; usually approved within minutes. Smaller runs can go ahead meanwhile."
  fi
}

raise() {
  az extension add --name quota --only-show-errors
  local scope="/subscriptions/$SUBSCRIPTION/providers/Microsoft.Compute/locations/$LOCATION"
  local esv6
  esv6=$(az vm list-usage -l "$LOCATION" --query "[?localName=='$FAMILY_E'].name.value | [0]" -o tsv)
  say "Asking for $NEED_E vCPU of $FAMILY_E ($esv6)"
  az quota update --resource-name "$esv6" --scope "$scope" --limit-object value="$NEED_E" \
    --resource-type dedicated --query "properties.limit.value" -o tsv
  quota
}

setup() {
  az account set --subscription "$SUBSCRIPTION"
  for p in Microsoft.Compute Microsoft.Network Microsoft.ManagedIdentity; do
    if [ "$(az provider show -n "$p" --query registrationState -o tsv)" != Registered ]; then
      say "Registering $p"
      az provider register -n "$p" --wait
    fi
  done

  say "Resource group $RESOURCE_GROUP ($LOCATION)"
  az group create -n "$RESOURCE_GROUP" -l "$LOCATION" --tags purpose=veromesh-capacity -o none

  say "Network: $VNET / $SUBNET, inbound closed ($NSG)"
  if ! az network nsg show -g "$RESOURCE_GROUP" -n "$NSG" -o none 2>/dev/null; then
    az network nsg create -g "$RESOURCE_GROUP" -n "$NSG" -o none  # default rules: no inbound from the internet
  fi
  if ! az network vnet show -g "$RESOURCE_GROUP" -n "$VNET" -o none 2>/dev/null; then
    az network vnet create -g "$RESOURCE_GROUP" -n "$VNET" --address-prefixes 10.42.0.0/24 \
      --subnet-name "$SUBNET" --subnet-prefixes 10.42.0.0/26 --network-security-group "$NSG" -o none
  fi

  say "GitHub identity $IDENTITY (OIDC for $GITHUB_REPO, environment $GITHUB_ENVIRONMENT)"
  if ! az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY" -o none 2>/dev/null; then
    az identity create -g "$RESOURCE_GROUP" -n "$IDENTITY" -o none
  fi
  # GitHub's token names the repository by name, or (newer) with the owner's and the
  # repository's numeric IDs as well: repo:owner@123/name@456:environment:capacity.
  # Both are accepted, so the sign-in works whichever GitHub sends.
  local ids subject_ids
  ids=$(curl -fsS "https://api.github.com/repos/$GITHUB_REPO" | python3 -c \
    'import json,sys; r=json.load(sys.stdin); print(r["owner"]["id"], r["id"])')
  subject_ids="repo:${GITHUB_REPO%%/*}@${ids% *}/${GITHUB_REPO#*/}@${ids#* }:environment:$GITHUB_ENVIRONMENT"
  local name subject
  for name in github-capacity github-capacity-ids; do
    subject="repo:$GITHUB_REPO:environment:$GITHUB_ENVIRONMENT"
    [ "$name" = github-capacity-ids ] && subject=$subject_ids
    if ! az identity federated-credential show -g "$RESOURCE_GROUP" --identity-name "$IDENTITY" -n "$name" -o none 2>/dev/null; then
      az identity federated-credential create -g "$RESOURCE_GROUP" --identity-name "$IDENTITY" -n "$name" \
        --issuer https://token.actions.githubusercontent.com --subject "$subject" \
        --audiences api://AzureADTokenExchange -o none
    fi
  done
  local principal scope
  principal=$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY" --query principalId -o tsv)
  scope=$(az group show -n "$RESOURCE_GROUP" --query id -o tsv)
  # Contributor on this resource group only: it can create and delete the test machines
  # here, and touches nothing else in the subscription.
  if [ -z "$(az role assignment list --assignee "$principal" --scope "$scope" --role Contributor --query '[0].id' -o tsv)" ]; then
    for _ in 1 2 3 4 5 6; do  # a new identity can take a minute to be visible to role assignment
      if az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
        --role Contributor --scope "$scope" -o none 2>/dev/null; then break; fi
      sleep 10
    done
  fi

  quota
  show
}

show() {
  local client tenant
  client=$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY" --query clientId -o tsv)
  tenant=$(az account show --query tenantId -o tsv)
  say "Set these in GitHub: $GITHUB_REPO > Settings > Environments > $GITHUB_ENVIRONMENT > Environment variables"
  cat <<EOF
  AZURE_CLIENT_ID        $client
  AZURE_TENANT_ID        $tenant
  AZURE_SUBSCRIPTION_ID  $SUBSCRIPTION
  AZURE_RESOURCE_GROUP   $RESOURCE_GROUP
  AZURE_LOCATION         $LOCATION

Or with the GitHub CLI (as a repository admin):
  gh api -X PUT repos/$GITHUB_REPO/environments/$GITHUB_ENVIRONMENT
  gh variable set AZURE_CLIENT_ID --env $GITHUB_ENVIRONMENT --repo $GITHUB_REPO --body $client
  gh variable set AZURE_TENANT_ID --env $GITHUB_ENVIRONMENT --repo $GITHUB_REPO --body $tenant
  gh variable set AZURE_SUBSCRIPTION_ID --env $GITHUB_ENVIRONMENT --repo $GITHUB_REPO --body $SUBSCRIPTION
  gh variable set AZURE_RESOURCE_GROUP --env $GITHUB_ENVIRONMENT --repo $GITHUB_REPO --body $RESOURCE_GROUP
  gh variable set AZURE_LOCATION --env $GITHUB_ENVIRONMENT --repo $GITHUB_REPO --body $LOCATION
EOF
}

case "${1:-all}" in
  all) setup ;;
  quota) az account set --subscription "$SUBSCRIPTION"; quota ;;
  raise) az account set --subscription "$SUBSCRIPTION"; raise ;;
  show) show ;;
  *) echo "usage: $0 [all|quota|raise|show]" >&2; exit 2 ;;
esac
