#!/usr/bin/env bash
# Clean up after the capacity runs, in Azure Cloud Shell (Bash).
#
#   ./teardown.sh list        what is in the resource group now, and what it costs to keep
#   ./teardown.sh machines    delete every test machine left behind (and its disk,
#                             network card and public address); keeps the setup
#   ./teardown.sh all         delete the whole resource group: machines, network and the
#                             GitHub identity (run setup.sh again to use the workflow again)
#
# The workflow deletes each machine when its run ends, even when the run fails; this is
# for a run that was cancelled half-way, or a machine kept on purpose (keep_machine).
set -euo pipefail

SUBSCRIPTION=${SUBSCRIPTION:-$(az account show --query id -o tsv)}
RESOURCE_GROUP=${RESOURCE_GROUP:-veromesh-capacity}
az account set --subscription "$SUBSCRIPTION"

machines() {
  az vm list -g "$RESOURCE_GROUP" --query "[?tags.purpose=='veromesh-capacity'].name" -o tsv
}

case "${1:-list}" in
  list)
    echo "Machines (each costs its hourly price while running; nothing once deleted):"
    az vm list -g "$RESOURCE_GROUP" -d \
      --query "[].{name:name, size:hardwareProfile.vmSize, state:powerState, run:tags.run, deleteAfter:tags.deleteAfter}" -o table
    echo
    echo "Everything else in $RESOURCE_GROUP (network and identity: free):"
    az resource list -g "$RESOURCE_GROUP" --query "[].{name:name, type:type}" -o table
    ;;
  machines)
    for vm in $(machines); do
      echo "Deleting $vm"
      ip=$(az vm show -g "$RESOURCE_GROUP" -n "$vm" --query tags.publicIp -o tsv)
      az vm delete -g "$RESOURCE_GROUP" -n "$vm" --yes --force-deletion true -o none
      [ -n "$ip" ] && az network public-ip delete -g "$RESOURCE_GROUP" -n "$ip" -o none || true
    done
    # Anything a deleted machine left: disks and network cards are deleted with it, public
    # addresses by name above; these catch a machine whose creation failed half-way.
    for id in $(az disk list -g "$RESOURCE_GROUP" --query "[?tags.purpose=='veromesh-capacity' && managedBy==null].id" -o tsv); do
      az disk delete --ids "$id" --yes -o none
    done
    for id in $(az network nic list -g "$RESOURCE_GROUP" --query "[?virtualMachine==null].id" -o tsv); do
      az network nic delete --ids "$id" -o none
    done
    for id in $(az network public-ip list -g "$RESOURCE_GROUP" --query "[?ipConfiguration==null].id" -o tsv); do
      az network public-ip delete --ids "$id" -o none
    done
    echo "Done. What is left:"
    az resource list -g "$RESOURCE_GROUP" --query "[].{name:name, type:type}" -o table
    ;;
  all)
    read -r -p "Delete resource group $RESOURCE_GROUP and everything in it? Type its name to confirm: " answer
    [ "$answer" = "$RESOURCE_GROUP" ] || { echo "Not deleted."; exit 1; }
    az group delete -n "$RESOURCE_GROUP" --yes
    echo "Deleted. Remove the AZURE_* variables from the GitHub environment, or run setup.sh again later."
    ;;
  *) echo "usage: $0 [list|machines|all]" >&2; exit 2 ;;
esac
