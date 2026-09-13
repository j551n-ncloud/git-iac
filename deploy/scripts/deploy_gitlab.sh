#!/usr/bin/env bash
set -euo pipefail

# The one stack CI will never deploy - FORBIDDEN_STACKS refuses it even
# if asked by name, because recreating the GitLab container would kill
# the pipeline running the recreate (see .gitlab-ci.yml and
# inventory/host_vars/tools.yml's header). This is the local
# ansible-playbook run the Infrastructure wiki page and those comments say to use
# instead. Two playbooks, both scoped to tools: the normal
# playbooks/deploy.yml (same -e flags CI's own deploy job passes) for
# the container itself, then playbooks/deploy_gitlab_config.yml for
# everything that has to be applied straight to gitlab.rb instead - see
# roles/gitlab_config for why GITLAB_OMNIBUS_CONFIG can't do that job.
#
# Run from anywhere inside the repo; cd's to the repo root itself so
# ansible.cfg's roles_path/inventory settings are in effect regardless
# of your current directory.
#
# Requires:
#   - SSH access to tools as the deploy user (same key CI uses, or
#     your own if it's authorized) - see roles/host_bootstrap.
#   - The vault password and the three PROXMOX_* values (required for
#     the pre-deploy snapshot, same as CI - every deploy here takes
#     one, no flag skips it). No pre-setup needed: a `.proxmox.json` at
#     the repo root (same shape CI's own .proxmox_creds step builds -
#     see .gitlab-ci.yml - and already gitignored) is used as-is if
#     present; otherwise anything not already in your environment gets
#     prompted for, secrets masked, and written only to a 0600 temp
#     file cleaned up on exit - never to .proxmox.json itself.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f .proxmox.json ]; then
  PROXMOX_JSON=.proxmox.json
  CLEANUP_PROXMOX_JSON=false
else
  if [ -z "${PROXMOX_API_URL:-}" ]; then
    read -r -p "PROXMOX_API_URL (e.g. https://192.0.2.x:8006): " PROXMOX_API_URL
  fi
  if [ -z "${PROXMOX_TOKEN_ID:-}" ]; then
    read -r -p "PROXMOX_TOKEN_ID (e.g. deploy@pve!ci): " PROXMOX_TOKEN_ID
  fi
  if [ -z "${PROXMOX_TOKEN_SECRET:-}" ]; then
    read -r -s -p "PROXMOX_TOKEN_SECRET: " PROXMOX_TOKEN_SECRET
    echo
  fi
  for v in PROXMOX_API_URL PROXMOX_TOKEN_ID PROXMOX_TOKEN_SECRET; do
    if [ -z "${!v:-}" ]; then
      echo "$v is empty - the pre-deploy snapshot cannot run without it." >&2
      exit 1
    fi
  done

  PROXMOX_JSON="$(mktemp)"
  CLEANUP_PROXMOX_JSON=true
  umask 077
  python3 - > "$PROXMOX_JSON" <<'PYEOF'
import json, os
print(json.dumps({
    "proxmox_api_url": os.environ["PROXMOX_API_URL"],
    "proxmox_api_token_id": os.environ["PROXMOX_TOKEN_ID"],
    "proxmox_api_token_secret": os.environ["PROXMOX_TOKEN_SECRET"],
}))
PYEOF
  umask 022
fi
trap '[ "$CLEANUP_PROXMOX_JSON" = true ] && rm -f "$PROXMOX_JSON"' EXIT

VAULT_ARGS=(--ask-vault-pass)
if [ -n "${ANSIBLE_VAULT_PASSWORD_FILE:-}" ]; then
  VAULT_ARGS=(--vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE")
fi

echo "Deploying gitlab on tools (local run, snapshot first)..."
# Two playbook FILES, one ansible-playbook RUN (it accepts several and
# plays them in sequence) - one vault-password prompt, not two.
# playbooks/deploy.yml is the generic one CI also uses for every other
# stack (container recreate + health-wait); playbooks/deploy_gitlab_
# config.yml is a completely different kind of operation (edits
# gitlab.rb on the host, then `gitlab-ctl reconfigure` - see
# roles/gitlab_config for why GITLAB_OMNIBUS_CONFIG can't do that job)
# and is kept as its own file rather than folded into the playbook
# every other stack's CI deploy depends on.
ansible-playbook -i inventory/ \
  --limit tools \
  playbooks/deploy.yml playbooks/deploy_gitlab_config.yml \
  -e stack_filter=gitlab \
  -e snapshot_before_deploy=true \
  -e @"$PROXMOX_JSON" \
  "${VAULT_ARGS[@]}"
