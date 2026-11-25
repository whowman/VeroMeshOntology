#!/usr/bin/env bash
# One-time Azure setup for VeroMesh's dev install, beside the assessment on its AKS
# cluster (issue #19), run in Azure Cloud Shell (Bash) by a subscription Owner after the
# assessment's own deploy/azure/setup.sh. Every step checks what exists first, so running
# it again is safe: it creates what is missing and leaves the rest alone.
#
#   ./setup.sh all        everything below, in order
#   ./setup.sh tls <cert.pem> <key.pem>   an origin certificate for the host, if the assessment's does not cover it
#   ./setup.sh show       what to send Scott and GitHub, and the sign-in details
#   ./setup.sh off        turn the ontology tool off: its node pool to 0 (the assessment keeps running)
#   ./setup.sh on         turn it back on (about 5-10 minutes until it answers)
#   ./setup.sh status     on or off, its nodes and pods
#   (the same three from GitHub: Actions > Ontology on/off, no approval needed)
#
# What it adds, in resource group VeroMeshAI (West US 2), reusing the assessment's
# cluster, registry, PostgreSQL server and gateway:
#   a node pool 'veromesh' (3 x B2s_v2, 64 GB disks: about $200 a month with storage)
#   . a Key Vault with generated secrets . a database and user on the PostgreSQL server for
#   the decision log . the namespace veromesh-dev and its Secrets . a listener on the
#   gateway for dev-ontology.veromesh.ai . two Entra apps (the API with its app roles,
#   the steward screen) . a GitHub deploy identity for this repository's azure-dev environment
set -euo pipefail

SUBSCRIPTION=${SUBSCRIPTION:-9f805da2-9785-4e0e-920c-156f7c96fb59}
RESOURCE_GROUP=VeroMeshAI
LOCATION=westus2
GITHUB_REPO=whowman/VeroMeshOntology
ENVIRONMENT=azure-dev
HOST=${HOST:-dev-ontology.veromesh.ai}
NAMESPACE=veromesh-dev
SUFFIX=$(printf %s "$SUBSCRIPTION" | sha256sum | cut -c1-6)
AKS=aks-veromesh
ACR=acrveromesh$SUFFIX
PG_SERVER=pg-veromesh-$SUFFIX
PG_ADMIN=pgadmin
ASSESSMENT_VAULT=kv-vmassess-$SUFFIX   # holds the PostgreSQL administrator's password
KV=kv-vmonto-dev-$SUFFIX
DB=veromesh_decisions_dev
DB_USER=veromesh_dev
POOL=veromesh
POOL_SIZE=${POOL_SIZE:-Standard_B2s_v2}
POOL_COUNT=${POOL_COUNT:-3}
GATEWAY_NS=assessment-gateway
GATEWAY=assessment-gateway
CERT_SECRET=${CERT_SECRET:-assessment-origin-cert}
ID_GITHUB=id-github-ontology
ID_POWER=id-github-ontology-power   # may only scale the veromesh node pool
POWER_ENVIRONMENT=azure-dev-power
API_APP="VeroMesh Ontology API (dev)"
UI_APP="VeroMesh steward screen (dev)"
ROLES=(admin steward connector reader agent)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    if (( n >= 20 )); then echo "giving up: $*" >&2; return 1; fi
    echo "  waiting for permissions to apply ($n)..."; sleep 15
  done
}
assign() { # role, scope, principal object id, principal type
  if [[ -z $(az role assignment list --assignee "$3" --role "$1" --scope "$2" --query '[0].id' -o tsv 2>/dev/null) ]]; then
    az role assignment create --role "$1" --scope "$2" --assignee-object-id "$3" --assignee-principal-type "$4" -o none
  fi
}
secret_set_if_missing() { # vault, name, value
  if ! exists az keyvault secret show --vault-name "$1" -n "$2"; then
    retry az keyvault secret set --vault-name "$1" -n "$2" --value "$3" -o none
    echo "  $2 created"
  fi
}
secret() { az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv; }
me() { az ad signed-in-user show --query id -o tsv; }
kube() {
  if ! kubectl config current-context 2>/dev/null | grep -q "$AKS"; then
    az aks get-credentials -g "$RESOURCE_GROUP" -n "$AKS" --overwrite-existing -o none
    kubelogin convert-kubeconfig -l azurecli
  fi
  retry kubectl get namespaces >/dev/null
}

# --------------------------------------------------------------------------- steps

step_prereqs() {
  say "Subscription, and the assessment's resources this reuses"
  az account set --subscription "$SUBSCRIPTION"
  exists az aks show -g "$RESOURCE_GROUP" -n "$AKS" || { echo "No cluster $AKS: run the assessment's deploy/azure/setup.sh first." >&2; exit 1; }
  exists az postgres flexible-server show -g "$RESOURCE_GROUP" -n "$PG_SERVER" || { echo "No PostgreSQL server $PG_SERVER." >&2; exit 1; }
}

step_quota() {
  say "vCPU quota for $POOL_COUNT more $POOL_SIZE nodes"
  # On a rerun the pool's own vCPUs are already counted as in use.
  if exists az aks nodepool show -g "$RESOURCE_GROUP" --cluster-name "$AKS" -n "$POOL"; then
    echo "  Node pool $POOL already exists; no more quota needed."
    return
  fi
  local need family used limit
  need=$((POOL_COUNT * $(az vm list-skus -l "$LOCATION" --size "$POOL_SIZE" --query "[0].capabilities[?name=='vCPUs'].value | [0]" -o tsv)))
  family=$(az vm list-skus -l "$LOCATION" --size "$POOL_SIZE" --query '[0].family' -o tsv)
  # One row per match, tab-separated (a flattened "| [0]" would print one value per line).
  read -r used limit < <(az vm list-usage -l "$LOCATION" --query "[?name.value=='$family'].[currentValue, limit]" -o tsv | head -n 1)
  if [[ -z "$used" || -z "$limit" ]]; then
    echo "  Could not read the $family quota in $LOCATION (az vm list-usage -l $LOCATION -o table)." >&2
    exit 1
  fi
  echo "  $family: $used of $limit vCPUs in use; this adds $need"
  if (( used + need > limit )); then
    echo "  Not enough quota: ask for $((used + need)) vCPUs in $family (Azure portal > Quotas), then run again." >&2
    exit 1
  fi
}

step_pool() {
  say "Node pool $POOL ($POOL_COUNT x $POOL_SIZE)"
  exists az aks nodepool show -g "$RESOURCE_GROUP" --cluster-name "$AKS" -n "$POOL" ||
    az aks nodepool add -g "$RESOURCE_GROUP" --cluster-name "$AKS" -n "$POOL" --mode User \
      --node-count "$POOL_COUNT" --node-vm-size "$POOL_SIZE" --os-sku AzureLinux \
      --node-osdisk-size 64 --labels workload=veromesh -o none
}

step_vault() {
  say "Key Vault $KV"
  exists az keyvault show -n "$KV" ||
    az keyvault create -g "$RESOURCE_GROUP" -n "$KV" -l "$LOCATION" --enable-rbac-authorization true \
      --retention-days 30 --enable-purge-protection true -o none
  assign "Key Vault Secrets Officer" "$(az keyvault show -n "$KV" --query id -o tsv)" "$(me)" User
  secret_set_if_missing "$KV" database-password "$(openssl rand -hex 24)"
  secret_set_if_missing "$KV" postgres-password "$(openssl rand -hex 16)"
  secret_set_if_missing "$KV" postgres-replication-password "$(openssl rand -hex 16)"
  if ! exists az keyvault secret show --vault-name "$KV" -n backup-key; then
    # Backups are encrypted to this SSH key; its private half stays in the vault.
    local dir
    dir=$(umask 077 && mktemp -d)
    ssh-keygen -q -t ed25519 -N '' -C veromesh-dev-backup -f "$dir/key"
    secret_set_if_missing "$KV" backup-key "$(cat "$dir/key")"
    secret_set_if_missing "$KV" backup-recipient "$(cat "$dir/key.pub")"
    rm -rf "$dir"
  fi
}

step_database() {
  say "Database $DB and user $DB_USER on $PG_SERVER (the decision log)"
  # Azure CLI 2.86 replaced --database-name/-d with --name/-n; older CLIs have only the former.
  local db_flag=--database-name
  az postgres flexible-server db create -h 2>/dev/null | grep -qE -- '^ *--name( |$)' && db_flag=--name
  exists az postgres flexible-server db show -g "$RESOURCE_GROUP" -s "$PG_SERVER" "$db_flag" "$DB" ||
    az postgres flexible-server db create -g "$RESOURCE_GROUP" -s "$PG_SERVER" "$db_flag" "$DB" -o none
  kube
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # The server is reachable only from the cluster's network: create the user from a pod.
  local admin_pw pw
  admin_pw=$(az keyvault secret show --vault-name "$ASSESSMENT_VAULT" -n postgres-admin-password --query value -o tsv)
  pw=$(secret database-password)
  kubectl -n "$NAMESPACE" delete pod veromesh-db-setup --ignore-not-found >/dev/null
  kubectl -n "$NAMESPACE" run veromesh-db-setup --restart=Never --rm -i --quiet \
    --image "$ACR.azurecr.io/library/postgres:16-alpine" \
    --env "PGPASSWORD=$admin_pw" --env "PGHOST=$PG_SERVER.postgres.database.azure.com" --env PGSSLMODE=require \
    --env "PGUSER=$PG_ADMIN" --env PGDATABASE=postgres -- sh -c "
      psql -v ON_ERROR_STOP=1 -c \"DO \\\$\\\$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$DB_USER') THEN
          CREATE ROLE $DB_USER LOGIN PASSWORD '$pw';
        ELSE ALTER ROLE $DB_USER PASSWORD '$pw'; END IF; END \\\$\\\$;\" &&
      psql -v ON_ERROR_STOP=1 -c 'GRANT ALL ON DATABASE $DB TO $DB_USER' &&
      psql -v ON_ERROR_STOP=1 -d $DB -c 'GRANT ALL ON SCHEMA public TO $DB_USER'"
}

step_secrets() {
  say "Secrets in namespace $NAMESPACE"
  kube
  local url
  url="postgresql://$DB_USER:$(secret database-password)@$PG_SERVER.postgres.database.azure.com:5432/$DB?sslmode=require"
  kubectl -n "$NAMESPACE" create secret generic veromesh-decision-log --from-literal=url="$url" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$NAMESPACE" create secret generic postgresql-secrets \
    --from-literal=postgres-password="$(secret postgres-password)" \
    --from-literal=replication-password="$(secret postgres-replication-password)" \
    --from-literal=password="$(secret postgres-password)" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # Backups go to a volume in the cluster for this dev install (not off-site).
  kubectl -n "$NAMESPACE" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: veromesh-backups, namespace: $NAMESPACE}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 8Gi}}}
YAML
  echo "  veromesh-decision-log, postgresql-secrets, the backup volume"
}

step_gateway() {
  say "Listener for $HOST on the assessment's gateway"
  kube
  if kubectl -n "$GATEWAY_NS" get gateway "$GATEWAY" -o jsonpath='{.spec.listeners[*].hostname}' | tr ' ' '\n' | grep -qx "$HOST"; then
    echo "  already there"
  else
    kubectl -n "$GATEWAY_NS" patch gateway "$GATEWAY" --type=json -p "[{\"op\":\"add\",\"path\":\"/spec/listeners/-\",\"value\":{
      \"name\":\"https-ontology-dev\",\"protocol\":\"HTTPS\",\"port\":443,\"hostname\":\"$HOST\",
      \"tls\":{\"mode\":\"Terminate\",\"certificateRefs\":[{\"name\":\"$CERT_SECRET\"}]},
      \"allowedRoutes\":{\"namespaces\":{\"from\":\"Selector\",\"selector\":{\"matchLabels\":{\"kubernetes.io/metadata.name\":\"$NAMESPACE\"}}}}}}]"
  fi
  # The assessment's setup.sh re-applies gateway.yaml with its own listeners only: run
  # this step again after './setup.sh gateway' there (README, "If the assessment's gateway is re-applied").
  local sans
  sans=$(kubectl -n "$GATEWAY_NS" get secret "$CERT_SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d |
    openssl x509 -noout -ext subjectAltName 2>/dev/null || true)
  if [[ $sans == *"$HOST"* || $sans == *"*.veromesh.ai"* ]]; then
    echo "  The origin certificate covers $HOST."
  else
    echo "  The origin certificate does not cover $HOST: ask Scott for one, then ./setup.sh tls <cert.pem> <key.pem>."
  fi
}

step_tls() {
  local cert=$1 key=$2
  say "Origin certificate for $HOST"
  openssl x509 -in "$cert" -noout -subject -enddate -ext subjectAltName
  [[ $(openssl x509 -in "$cert" -noout -pubkey | openssl sha256) == $(openssl pkey -in "$key" -pubout | openssl sha256) ]] ||
    { echo "the key does not belong to the certificate" >&2; exit 1; }
  kube
  kubectl -n "$GATEWAY_NS" create secret tls ontology-origin-cert --cert "$cert" --key "$key" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$GATEWAY_NS" patch gateway "$GATEWAY" --type=json -p "$(kubectl -n "$GATEWAY_NS" get gateway "$GATEWAY" -o json |
    python3 -c "import json,sys; g=json.load(sys.stdin); i=[l['name'] for l in g['spec']['listeners']].index('https-ontology-dev'); print(json.dumps([{'op':'replace','path':f'/spec/listeners/{i}/tls/certificateRefs','value':[{'name':'ontology-origin-cert'}]}]))")"
  echo "  Installed. You can now delete $cert and $key from Cloud Shell."
}

# GitHub may present the repository by its fixed IDs instead of its name
# (repo:<owner>@<owner id>/<repo>@<repo id>:...), as the assessment's setup.sh also allows.
trust_github() { # identity, credential name, environment
  local ids
  exists az identity federated-credential show -g "$RESOURCE_GROUP" --identity-name "$1" -n "$2" ||
    az identity federated-credential create -g "$RESOURCE_GROUP" --identity-name "$1" -n "$2" \
      --issuer https://token.actions.githubusercontent.com --subject "repo:$GITHUB_REPO:environment:$3" \
      --audiences api://AzureADTokenExchange -o none
  ids=$(gh api "repos/$GITHUB_REPO" --jq '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"' 2>/dev/null || true)
  if [[ -n $ids ]]; then
    exists az identity federated-credential show -g "$RESOURCE_GROUP" --identity-name "$1" -n "$2-ids" ||
      az identity federated-credential create -g "$RESOURCE_GROUP" --identity-name "$1" -n "$2-ids" \
        --issuer https://token.actions.githubusercontent.com --subject "repo:$ids:environment:$3" \
        --audiences api://AzureADTokenExchange -o none
  else
    echo "  (sign in with 'gh auth login' and run again to also trust GitHub's ID-based subject)"
  fi
}

graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" ${3:+--body "$3"} --headers Content-Type=application/json; }

step_entra() {
  say "Entra apps: '$API_APP' (app roles) and '$UI_APP' (steward screen sign-in)"
  local api_id api_obj ui_id ui_obj scope_id roles_json
  api_id=$(az ad app list --display-name "$API_APP" --query '[0].appId' -o tsv)
  if [[ -z $api_id ]]; then
    api_id=$(az ad app create --display-name "$API_APP" --sign-in-audience AzureADMyOrg --query appId -o tsv) || {
      echo "Registering apps needs the Entra role Application Developer (or Application Administrator)." >&2; exit 1; }
  fi
  api_obj=$(az ad app show --id "$api_id" --query id -o tsv)
  if [[ -z $(az ad app show --id "$api_id" --query 'api.oauth2PermissionScopes[0].id' -o tsv) ]]; then
    scope_id=$(cat /proc/sys/kernel/random/uuid)
    roles_json=$(for r in "${ROLES[@]}"; do
      printf '{"id":"%s","allowedMemberTypes":["User","Application"],"displayName":"VeroMesh %s","description":"VeroMesh %s role","value":"VeroMesh.%s","isEnabled":true},' \
        "$(cat /proc/sys/kernel/random/uuid)" "$r" "$r" "$r"; done | sed 's/,$//')
    graph PATCH "applications/$api_obj" "{\"identifierUris\":[\"api://$api_id\"],\"appRoles\":[$roles_json],
      \"api\":{\"requestedAccessTokenVersion\":2,\"oauth2PermissionScopes\":[{\"id\":\"$scope_id\",\"adminConsentDisplayName\":\"Use VeroMesh as you\",
      \"adminConsentDescription\":\"Lets the app call VeroMesh as the signed-in person.\",\"userConsentDisplayName\":\"Use VeroMesh as you\",
      \"userConsentDescription\":\"Lets the app call VeroMesh as you.\",\"isEnabled\":true,\"type\":\"User\",\"value\":\"access_as_user\"}]}}"
  fi
  exists az ad sp show --id "$api_id" || az ad sp create --id "$api_id" -o none
  scope_id=$(az ad app show --id "$api_id" --query "api.oauth2PermissionScopes[?value=='access_as_user'].id | [0]" -o tsv)
  ui_id=$(az ad app list --display-name "$UI_APP" --query '[0].appId' -o tsv)
  [[ -n $ui_id ]] || ui_id=$(az ad app create --display-name "$UI_APP" --sign-in-audience AzureADMyOrg --query appId -o tsv)
  ui_obj=$(az ad app show --id "$ui_id" --query id -o tsv)
  graph PATCH "applications/$ui_obj" "{\"spa\":{\"redirectUris\":[\"https://$HOST/steward/\",\"https://$HOST/admin/\"]},
    \"requiredResourceAccess\":[{\"resourceAppId\":\"$api_id\",\"resourceAccess\":[{\"id\":\"$scope_id\",\"type\":\"Scope\"}]}]}"
  exists az ad sp show --id "$ui_id" || az ad sp create --id "$ui_id" -o none
  # You, as administrator and steward of the dev install.
  local sp role
  sp=$(az ad sp show --id "$api_id" --query id -o tsv)
  for r in admin steward; do
    role=$(az ad app show --id "$api_id" --query "appRoles[?value=='VeroMesh.$r'].id | [0]" -o tsv)
    if [[ -z $(az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$sp/appRoleAssignedTo" \
      --query "value[?principalId=='$(me)' && appRoleId=='$role'].id | [0]" -o tsv) ]]; then
      graph POST "servicePrincipals/$sp/appRoleAssignedTo" "{\"principalId\":\"$(me)\",\"resourceId\":\"$sp\",\"appRoleId\":\"$role\"}" >/dev/null
    fi
  done
  echo "  API: $api_id; steward screen: $ui_id; you hold VeroMesh.admin and VeroMesh.steward."
}

step_github() {
  say "GitHub deploy identity ($ID_GITHUB): only this repository's $ENVIRONMENT environment"
  exists az identity show -g "$RESOURCE_GROUP" -n "$ID_GITHUB" || az identity create -g "$RESOURCE_GROUP" -n "$ID_GITHUB" -l "$LOCATION" -o none
  local principal cluster_id
  principal=$(az identity show -g "$RESOURCE_GROUP" -n "$ID_GITHUB" --query principalId -o tsv)
  cluster_id=$(az aks show -g "$RESOURCE_GROUP" -n "$AKS" --query id -o tsv)
  trust_github "$ID_GITHUB" github-env "$ENVIRONMENT"
  assign AcrPush "$(az acr show -n "$ACR" --query id -o tsv)" "$principal" ServicePrincipal
  assign "Azure Kubernetes Service Cluster User Role" "$cluster_id" "$principal" ServicePrincipal
  assign "Azure Kubernetes Service RBAC Admin" "$cluster_id/namespaces/$NAMESPACE" "$principal" ServicePrincipal
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    gh api -X PUT "repos/$GITHUB_REPO/environments/$ENVIRONMENT" \
      -f "reviewers[][type]=User" -F "reviewers[][id]=$(gh api user --jq .id)" >/dev/null &&
      echo "  Environment $ENVIRONMENT: each deploy waits for your approval."
    while IFS='=' read -r name value; do gh variable set "$name" --repo "$GITHUB_REPO" --env "$ENVIRONMENT" --body "$value"; done <<<"$(variables)"
    echo "  Variables set on the $ENVIRONMENT environment."
  else
    echo "  Sign in with 'gh auth login' and run again, or add what './setup.sh show' prints as $ENVIRONMENT environment variables."
  fi
}

step_power_identity() {
  say "On/off identity ($ID_POWER): may scale the $POOL node pool and nothing else"
  exists az identity show -g "$RESOURCE_GROUP" -n "$ID_POWER" || az identity create -g "$RESOURCE_GROUP" -n "$ID_POWER" -l "$LOCATION" -o none
  local principal cluster_id
  principal=$(az identity show -g "$RESOURCE_GROUP" -n "$ID_POWER" --query principalId -o tsv)
  cluster_id=$(az aks show -g "$RESOURCE_GROUP" -n "$AKS" --query id -o tsv)
  trust_github "$ID_POWER" github-power "$POWER_ENVIRONMENT"
  # Reading the cluster, and changing the one node pool: not the assessment's nodes.
  assign "Azure Kubernetes Service Cluster User Role" "$cluster_id" "$principal" ServicePrincipal
  assign "Azure Kubernetes Service Contributor Role" "$cluster_id/agentPools/$POOL" "$principal" ServicePrincipal
  assign "Azure Kubernetes Service RBAC Reader" "$cluster_id/namespaces/$NAMESPACE" "$principal" ServicePrincipal
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    # No reviewer (turning it on or off is yours to do at any time), from main only.
    gh api -X PUT "repos/$GITHUB_REPO/environments/$POWER_ENVIRONMENT" --input - >/dev/null <<JSON
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
    gh api -X POST "repos/$GITHUB_REPO/environments/$POWER_ENVIRONMENT/deployment-branch-policies" -f name=main >/dev/null 2>&1 || true
    while IFS='=' read -r name value; do gh variable set "$name" --repo "$GITHUB_REPO" --env "$POWER_ENVIRONMENT" --body "$value"; done <<V
AZURE_TENANT_ID=$(az account show --query tenantId -o tsv)
AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION
AZURE_CLIENT_ID=$(az identity show -g "$RESOURCE_GROUP" -n "$ID_POWER" --query clientId -o tsv)
AKS_RESOURCE_GROUP=$RESOURCE_GROUP
AKS_NAME=$AKS
NODE_POOL=$POOL
NODE_COUNT=$POOL_COUNT
NAMESPACE=$NAMESPACE
V
    echo "  Environment $POWER_ENVIRONMENT and its variables set: Actions > Ontology on/off."
  fi
}

step_power() { # off | on | status
  az account set --subscription "$SUBSCRIPTION"
  local count
  count=$(az aks nodepool show -g "$RESOURCE_GROUP" --cluster-name "$AKS" -n "$POOL" --query count -o tsv)
  case $1 in
    off)
      say "Turning the ontology tool off (node pool $POOL to 0; the assessment keeps running)"
      if [[ $count == 0 ]]; then echo "  already off"; return; fi
      az aks nodepool scale -g "$RESOURCE_GROUP" --cluster-name "$AKS" -n "$POOL" --node-count 0 -o none
      echo "  Off. While off it costs about \$8 a month (the disks that keep its data)." ;;
    on)
      say "Turning the ontology tool on ($POOL_COUNT nodes)"
      if [[ $count == "$POOL_COUNT" ]]; then echo "  already on"; else
        az aks nodepool scale -g "$RESOURCE_GROUP" --cluster-name "$AKS" -n "$POOL" --node-count "$POOL_COUNT" -o none
      fi
      kube
      echo "  Nodes up; waiting for VeroMesh to answer..."
      kubectl -n "$NAMESPACE" rollout status deploy/veromesh-api --timeout=15m &&
        echo "  On: https://$HOST/steward/ (DataHub may take a few more minutes)." ;;
    status)
      say "Ontology tool: $([[ $count == 0 ]] && echo off || echo "on, $count nodes")"
      if [[ $count != 0 ]]; then kube; kubectl -n "$NAMESPACE" get pods -o wide; fi ;;
  esac
}

variables() {
  cat <<V
AZURE_TENANT_ID=$(az account show --query tenantId -o tsv)
AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION
AZURE_CLIENT_ID=$(az identity show -g "$RESOURCE_GROUP" -n "$ID_GITHUB" --query clientId -o tsv)
AKS_RESOURCE_GROUP=$RESOURCE_GROUP
AKS_NAME=$AKS
ACR_NAME=$ACR
NAMESPACE=$NAMESPACE
HOST=$HOST
API_CLIENT_ID=$(az ad app list --display-name "$API_APP" --query '[0].appId' -o tsv)
UI_CLIENT_ID=$(az ad app list --display-name "$UI_APP" --query '[0].appId' -o tsv)
BACKUP_RECIPIENT=$(secret backup-recipient)
V
}

step_show() {
  say "For Scott (Cloudflare)"
  echo "  A proxied DNS record $HOST -> $(az network public-ip show -g "$RESOURCE_GROUP" -n pip-assessment --query ipAddress -o tsv) (the assessment's address)"
  say "GitHub ($ENVIRONMENT environment variables)"
  variables | sed 's/^/  /'
  say "After the first deploy"
  echo "  https://$HOST/steward/ (sign in with your Entra account) and https://$HOST/docs"
}

case "${1:-}" in
  all)
    step_prereqs; step_quota; step_pool; step_vault; step_database; step_secrets; step_gateway
    step_entra; step_github; step_power_identity; step_show
    say "Done. Next: Scott's DNS record, then Actions > Deploy to Azure (dev) > Run workflow."
    ;;
  tls) [[ $# -eq 3 ]] || { echo "usage: $0 tls <cert.pem> <key.pem>" >&2; exit 2; }; az account set --subscription "$SUBSCRIPTION"; step_tls "$2" "$3" ;;
  gateway) az account set --subscription "$SUBSCRIPTION"; step_gateway ;;
  show) az account set --subscription "$SUBSCRIPTION"; step_show ;;
  off | on | status) step_power "$1" ;;
  *) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 2 ;;
esac
