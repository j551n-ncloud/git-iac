#!/usr/bin/env bash
# Creates the Proxmox API token that bin/kickstart, playbooks/provision.yml,
# patch.yml, dist_upgrade.yml and the snapshot reports use, with only the
# privileges they need, and prints a .proxmox.json template.
#
# Run as root on ONE Proxmox node (users, roles and ACLs live in the
# cluster-wide /etc/pve, so one node is enough):
#
#   bash proxmox-api-token.sh > .proxmox.json     # on the node, then copy the file
#   bash proxmox-api-token.sh [options]
#
# Options:
#   --user NAME@REALM   user to create        (default: infra@pve)
#   --token NAME        API token name        (default: ansible)
#   --role NAME         role to create/update (default: InfraAutomation)
#   --path PATH         ACL path              (default: /, whole cluster)
#                       Narrower: /pool/<pool> (put the guests in a pool and
#                       add /storage/<id> and /sdn/zones/localnetwork/<bridge>
#                       by hand with `pveum acl modify`).
#   --url URL           API URL for the template (default: https://<this node's IP>:8006)
#   --rotate            delete the existing token and create a new one
#   -h, --help          this text
#
# The token secret is printed ONCE by Proxmox. This script puts it on
# stdout, inside the JSON, and everything else on stderr, so redirecting
# stdout captures just the file. Store it as .proxmox.json (chmod 600,
# gitignored), or in a password manager. NEVER commit it. If you lose it,
# run again with --rotate.
#
# Idempotent: the role is updated in place, an existing user is kept, the
# ACL is set again (a no-op if unchanged), and an existing token is left
# alone unless you pass --rotate (its secret cannot be shown again).
#
# Privileges, and why each is there (names as in Proxmox VE 8/9):
#   VM.Allocate           create and delete guests
#   VM.Audit              read guest state and config (also cluster/resources)
#   VM.Config.CPU/Memory/Disk/Network/Options
#                         set what provision.yml sets at creation (cores,
#                         memory, swap, rootfs, net0, DNS, features, tags,
#                         onboot)
#   VM.PowerMgmt          start the guest after the raw lxc.* lines are written
#   VM.Snapshot           create and delete snapshots (patch.yml, dist_upgrade.yml)
#   Datastore.AllocateSpace  allocate the root disk on the storage
#   Datastore.Audit       see storages and the CT template on them
#   SDN.Use               attach the guest to a bridge (Proxmox VE 8 and
#                         later; on 7 the bridge needs Sys.Modify instead)
#   Sys.Audit             read nodes and task status (the playbooks poll tasks)
# Deliberately NOT granted: VM.Snapshot.Rollback (a rollback is a human
# decision), Sys.Modify, Permissions.Modify, Datastore.Allocate.
#
# Things to check on your version, because they come from memory of the
# Proxmox docs and were not verified against a live system:
#   * Privileged containers (k3s nodes) and feature flags other than
#     nesting may be allowed for root@pam only. If creating a k3s LXC
#     answers 403, create those with a root@pam token or by hand.
#   * Reading a task's status needs Sys.Audit on the node (granted here)
#     or being the task's owner.
#   * Run `pveum role list` and `pveum acl list` afterwards to see the result.
set -euo pipefail

PRIVS="VM.Allocate,VM.Audit,VM.Config.CPU,VM.Config.Memory,VM.Config.Disk,VM.Config.Network,VM.Config.Options,VM.PowerMgmt,VM.Snapshot,Datastore.AllocateSpace,Datastore.Audit,SDN.Use,Sys.Audit"

user="infra@pve"
token="ansible"
role="InfraAutomation"
path="/"
url=""
rotate=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
log() { echo "$*" >&2; }
die() { log "error: $*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --user)   user=${2:?}; shift 2 ;;
    --token)  token=${2:?}; shift 2 ;;
    --role)   role=${2:?}; shift 2 ;;
    --path)   path=${2:?}; shift 2 ;;
    --url)    url=${2:?}; shift 2 ;;
    --rotate) rotate=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root on a Proxmox node"
command -v pveum >/dev/null || die "pveum not found: this is not a Proxmox VE node"
command -v python3 >/dev/null || die "python3 not found (used to read pveum's JSON)"
case "$user" in *@*) ;; *) die "--user must look like name@realm, e.g. infra@pve" ;; esac

tokenid="$user!$token"
[ -n "$url" ] || url="https://$(hostname -I | awk '{print $1}'):8006"

# True if the JSON on stdin has an element whose field $1 equals $2.
has() {
  python3 -c 'import json,sys; sys.exit(0 if any(e.get(sys.argv[1])==sys.argv[2] for e in json.load(sys.stdin)) else 1)' "$1" "$2"
}

# --- role: create, or update the privileges of an existing one -----------
if pveum role list --output-format json | has roleid "$role"; then
  log "role $role exists, updating its privileges"
  pveum role modify "$role" --privs "$PRIVS"
else
  log "creating role $role"
  pveum role add "$role" --privs "$PRIVS"
fi

# --- user: keep an existing one ------------------------------------------
if pveum user list --output-format json | has userid "$user"; then
  log "user $user exists, keeping it"
else
  log "creating user $user (no password: it only owns the token)"
  pveum user add "$user" --comment "Ansible infra automation"
fi

# --- token: privilege separation on, so it only has what its ACL says ----
have_token=0
if pveum user token list "$user" --output-format json | has tokenid "$token"; then
  have_token=1
fi
if [ "$have_token" -eq 1 ] && [ "$rotate" -eq 1 ]; then
  log "removing token $tokenid (--rotate)"
  pveum user token remove "$user" "$token"
  have_token=0
fi

# --- ACL: with privilege separation the effective permissions are the
# intersection of the user's and the token's, so both get the role. The
# user's ACL goes in now; the token's after the token exists (an ACL for
# a token that does not exist yet is rejected, and removing a token
# removes its ACL) -----------------------------------------------------
log "granting role $role on $path to $user"
pveum acl modify "$path" --roles "$role" --users "$user" --propagate 1

secret="REPLACE_WITH_THE_TOKEN_SECRET"
if [ "$have_token" -eq 0 ]; then
  log "creating token $tokenid"
  out=$(pveum user token add "$user" "$token" --privsep 1 --output-format json)
  secret=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')
  log ""
  log "The secret is in the JSON on stdout below. Proxmox will not show it again."
else
  log ""
  log "Token $tokenid already exists, so its secret cannot be shown again."
  log "The template below has a placeholder. Use --rotate for a new token."
fi

log "granting role $role on $path to token $tokenid"
pveum acl modify "$path" --roles "$role" --tokens "$tokenid" --propagate 1

cat <<JSON
{
  "proxmox_api_url": "$url",
  "proxmox_api_token_id": "$tokenid",
  "proxmox_api_token_secret": "$secret"
}
JSON

log ""
log "Next: save the JSON as .proxmox.json in the infra repo (chmod 600), never commit it."
log "Check: curl -sk -H 'Authorization: PVEAPIToken=$tokenid=<secret>' $url/api2/json/version"
