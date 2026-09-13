#!/usr/bin/env bash
# Add a new image source, fully wired up: the submodule, plus a preview
# of what the pipeline will auto-discover to build from it. Run from
# the build repo root.
#
# Usage: scripts/add-submodule.sh <repo-url> [name]
# name defaults to the repo's own basename (strips a trailing .git).
set -euo pipefail

url="${1:?usage: add-submodule.sh <repo-url> [name]}"
name="${2:-$(basename "$url" .git)}"
path="projects/$name"

if [ -e "$path" ]; then
  echo "$path already exists." >&2
  exit 1
fi

git submodule add "$url" "$path"
git add "$path" .gitmodules

images="$(scripts/discover-images.sh "$path" || true)"
if [ -z "$images" ]; then
  echo
  echo "Warning: no Dockerfile found under $path - nothing here is buildable yet." >&2
  echo "That's fine if this submodule is a work in progress; the pipeline just" >&2
  echo "won't build anything from it until a Dockerfile shows up." >&2
else
  echo
  echo "Discovered image(s) - the pipeline will build these whenever this"
  echo "submodule's pointer moves, no further registration needed:"
  echo "$images" | awk '{print "  " $1 " (from " $2 ")"}'
fi

echo
echo "Next: git commit, then push with scripts/sync-submodule.sh $name"
echo "To mirror it to GitHub as well:"
echo "  scripts/add-mirror.sh <gitlab-project-path> <github-owner/repo>"
