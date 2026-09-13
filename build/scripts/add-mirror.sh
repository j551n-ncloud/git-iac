#!/usr/bin/env bash
# Mirror a GitLab repo to GitHub, end to end, with no stored token.
#
# GitLab generates its own SSH keypair for a push mirror and keeps the
# private half; only the public half needs to reach GitHub, as a deploy
# key on that one repo. That is a narrower credential than a personal
# access token and there is nothing to rotate here.
#
# Usage: scripts/add-mirror.sh <gitlab-project-path> <github-owner/repo>
#   e.g. scripts/add-mirror.sh example-group/docs-site example-org/docs-site
#
# Needs glab and gh, both authenticated. Idempotent: re-running against
# an existing mirror or deploy key reports and changes nothing.
set -euo pipefail

gl="${1:?usage: add-mirror.sh <gitlab-project-path> <github-owner/repo>}"
gh_repo="${2:?usage: add-mirror.sh <gitlab-project-path> <github-owner/repo>}"
enc="${gl//\//%2F}"
url="ssh://git@github.com/${gh_repo}.git"

id="$(glab api "projects/$enc/remote_mirrors" \
      | python3 -c "import sys,json;print(next((m['id'] for m in json.load(sys.stdin)
                     if m['url'].rstrip('/').endswith('$gh_repo.git')), ''))")"

if [ -n "$id" ]; then
  echo "mirror already exists (id=$id)"
else
  id="$(printf '%s' "{\"url\":\"$url\",\"enabled\":true,\"auth_method\":\"ssh_public_key\",\"only_protected_branches\":false,\"keep_divergent_refs\":false}" \
        | glab api --method POST "projects/$enc/remote_mirrors" --input - \
            -H "Content-Type: application/json" \
        | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")"
  echo "created mirror id=$id -> $url"
fi

# Not in the mirror object itself - it has its own endpoint, which is
# the bit that makes this scriptable at all.
key="$(glab api "projects/$enc/remote_mirrors/$id/public_key" \
       | python3 -c "import sys,json;print(json.load(sys.stdin)['public_key'].strip())")"

if gh api "repos/$gh_repo/keys" --jq '.[].key' | grep -qF "$(printf '%s' "$key" | awk '{print $2}')"; then
  echo "deploy key already on $gh_repo"
else
  # read_only=false, or the mirror authenticates and then cannot push.
  gh api "repos/$gh_repo/keys" -f title="GitLab push mirror ($gl)" \
     -f key="$key" -F read_only=false >/dev/null
  echo "added deploy key to $gh_repo (write access)"
fi

echo
echo "Verify after the next push to GitLab:"
echo "  glab api projects/$enc/remote_mirrors/$id | python3 -m json.tool | grep -E 'update_status|last_error'"
