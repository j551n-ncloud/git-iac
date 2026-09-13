#!/usr/bin/env bash
# Push a submodule's own pending commits to its GitLab remote, then
# bump/commit/push its pointer here. Run from the build repo root
# after committing inside projects/<name> - collapses what was
# previously several manual steps (push submodule to each mirror, cd
# back, git add the gitlink, commit, push here) into one.
#
# Usage: scripts/sync-submodule.sh <name> ["commit message"]
set -euo pipefail

name="${1:?usage: sync-submodule.sh <name> [\"commit message\"]}"
msg="${2:-${name}: bump submodule}"
sub="projects/$name"

if [ ! -d "$sub" ]; then
  echo "no such submodule: $sub" >&2
  exit 1
fi

# One push target: GitLab. Each submodule's GitLab project push-mirrors
# to GitHub (scripts/add-mirror.sh), so there is nothing to fan out to
# here.
url="$(git config -f .gitmodules --get "submodule.$sub.url")"
if [ -z "$url" ]; then
  echo "no url for '$sub' in .gitmodules - is it registered?" >&2
  exit 1
fi

echo "pushing $sub -> $url"
git -C "$sub" push "$url" main

git add "$sub"
if git diff --cached --quiet -- "$sub"; then
  echo "$sub: pointer unchanged, nothing to commit here."
  exit 0
fi

git commit -m "$msg"
git push origin main
