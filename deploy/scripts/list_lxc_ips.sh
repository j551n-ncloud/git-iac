#!/usr/bin/env bash
set -euo pipefail

# Run as root on any Proxmox node that is currently up. The cluster API
# proxies /nodes/<node>/... calls to whichever node actually holds each
# container, so this doesn't need to run separately per node - it only
# fails for containers on a node that's genuinely offline.
#
# Lists every LXC in the cluster with node, vmid, name, and primary
# IPv4 (via the lxc/<vmid>/interfaces endpoint - no guest agent
# required), formatted as inventory/hosts.ini lines. Copy/paste the
# output straight into the right [staging]/[production] section.

pvesh get /cluster/resources --type vm --output-format json \
  | jq -r '.[] | select(.type=="lxc") | [.node, .vmid, .name, .status] | @tsv' \
  | while IFS=$'\t' read -r node vmid name status; do
      if [ "$status" != "running" ]; then
        echo "# $name (vmid $vmid, node $node): status=$status, skipped"
        continue
      fi
      ip=$(pvesh get "/nodes/$node/lxc/$vmid/interfaces" --output-format json 2>/dev/null \
        | jq -r '.[] | select(.name=="eth0") | .inet // empty' \
        | cut -d/ -f1)
      if [ -z "$ip" ]; then
        echo "# $name (vmid $vmid, node $node): no eth0 IPv4 found (node unreachable, or a different iface name), skipped"
      else
        printf '%s ansible_host=%s  # vmid %s, node %s\n' "$name" "$ip" "$vmid" "$node"
      fi
    done
