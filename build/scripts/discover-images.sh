#!/bin/sh
# POSIX sh, not bash - alpine/git (detect-changes' image) doesn't have
# bash, only ash/sh.
#
# Find every buildable image under a submodule: any directory containing
# a Dockerfile, at the submodule root or nested (a monorepo like
# dashboard). Replaces hand-maintaining images.txt - a Dockerfile showing
# up is the only registration a new image needs.
#
# Asks git what exists rather than walking the filesystem. A tracked
# file is exactly the definition wanted here, and it settles three
# cases a `find` would get wrong, with no blocklist to maintain:
#   - a dependency tree's own Dockerfile (projects/docs-site has a
#     node_modules on disk today) is gitignored, so git never lists it
#   - a Dockerfile someone is still writing locally isn't committed, so
#     a pipeline run never pushes it on your behalf
#   - vendored third-party code (dashboard's vendored-lib) is either gitignored
#     or a nested submodule, and a submodule appears as a single gitlink
#     entry, never as the files inside it
#
# Usage: discover-images.sh <submodule-path>
# Output: one "name context submodule" row per image, same three
# columns build-and-push's loop already expects - dockerfile is always
# literally "Dockerfile" in this scheme, no per-row override needed.
set -eu

root="${1:?usage: discover-images.sh <submodule-path>}"
# Callers pass a path straight from .gitmodules, which never has a
# trailing slash - but one silently breaks every derived name below
# rather than failing, so normalise instead of trusting that.
root="${root%/}"
sub_name="$(basename "$root")"

if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "discover-images.sh: $root is not a git work tree." >&2
  echo "  A submodule always is one by the time either caller gets here" >&2
  echo "  (add-submodule.sh runs after 'git submodule add', build-and-push" >&2
  echo "  after 'git submodule update --init'), so this means the checkout" >&2
  echo "  didn't happen - building nothing would hide that, not handle it." >&2
  exit 1
fi

rows="$(mktemp)"
trap 'rm -f "$rows"' EXIT

# ls-files' own pathspec globbing would need :(glob) magic to stop `*`
# matching across `/`, and would still match a file merely ending in
# "Dockerfile". Filtering on the full path is shorter and says exactly
# what it means: the basename is Dockerfile, anywhere in the tree.
git -C "$root" ls-files | grep -E '(^|/)Dockerfile$' | sort | while IFS= read -r rel; do
  subdir="$(dirname "$rel")"
  if [ "$subdir" = "." ]; then
    name="$sub_name"
    ctx="$root"
  else
    name="${sub_name}-$(printf '%s' "$subdir" | tr '/' '-')"
    ctx="$root/$subdir"
  fi
  echo "$name $ctx $root"
done > "$rows"

# Two contexts deriving one name (a-b/Dockerfile and a/b/Dockerfile
# both give <sub>-a-b) would push to a single Harbor tag, the second
# build silently overwriting the first. Nothing downstream could detect
# that afterwards, so refuse here rather than pick a winner.
dupes="$(awk '{print $1}' "$rows" | sort | uniq -d)"
if [ -n "$dupes" ]; then
  echo "discover-images.sh: one image name from two build contexts under $root:" >&2
  for name in $dupes; do
    echo "  $name" >&2
    awk -v n="$name" '$1 == n {print "    " $2}' "$rows" >&2
  done
  echo "  Both would push to the same Harbor tag. Rename one directory." >&2
  exit 1
fi

cat "$rows"
