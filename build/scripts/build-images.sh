#!/bin/sh
# POSIX sh - runs in build-ci-image (FROM docker:27, alpine/ash).
#
# Build and push every image belonging to the given submodules. Lifted
# out of .gitlab-ci.yml so the automatic job and the manual rebuild job
# run the same code rather than two copies of it that drift, and so the
# decisions below (what to skip, when to refuse) can be tested without
# a runner.
#
# Usage: build-images.sh <file listing submodule paths, one per line>
# Writes: built_images.env  (NAME_IMAGE=<ref> per image, for
#         open_deploy_mr.py - a plain file, deliberately not a dotenv
#         report, whose key validation rejects hyphenated image names)
#         built_submodules.tsv  (path, full SHA, remote URL per submodule
#         that moved this run, also for open_deploy_mr.py - see below)
#
# Required env: HARBOR_URL HARBOR_PROJECT
# Optional env:
#   FORCE_REBUILD=true   rebuild and overwrite a tag Harbor already has
#   BUILD_CACHE=false    skip the registry-backed layer cache
#   DOCKER=<path>        docker CLI to use (tests point this at a stub)
set -eu

list="${1:?usage: build-images.sh <submodules-file>}"
docker="${DOCKER:-docker}"

for v in HARBOR_URL HARBOR_PROJECT; do
  eval "val=\${$v:-}"
  if [ -z "$val" ]; then
    echo "$v is empty - see .gitlab-ci.yml's header comment." >&2
    exit 1
  fi
done

if [ ! -s "$list" ]; then
  echo "no submodules to build - nothing to do."
  : > built_images.env
  : > built_submodules.tsv
  exit 0
fi

# For every submodule in the list, not just the ones that produced an
# image: a git_stack pins the submodule's commit, so its pin can need to
# move even when its images were skipped as already-in-Harbor.
#
# Full SHA (this becomes a `version:` in deploy, where an abbreviation
# could collide) and the .gitmodules URL (what deploy's `repo:` is
# matched against).
: > built_submodules.tsv
while IFS= read -r sub; do
  [ -n "$sub" ] || continue
  full="$(git rev-parse "HEAD:$sub")"
  url="$(git config -f .gitmodules --get "submodule.$sub.url" || echo "")"
  printf '%s\t%s\t%s\n' "$sub" "$full" "$url" >> built_submodules.tsv
done < "$list"

echo "built_submodules.tsv:"
sed 's/^/  /' built_submodules.tsv

# Discovery needs the real files on disk, which is why this runs in the
# build job and not in detect-changes: by now the SSH-authenticated
# submodule checkout has happened. A Dockerfile appearing under a
# submodule is the only registration an image needs.
images="$(mktemp)"
trap 'rm -f "$images"' EXIT
while IFS= read -r sub; do
  [ -n "$sub" ] || continue
  sh scripts/discover-images.sh "$sub" >> "$images"
done < "$list"

if [ ! -s "$images" ]; then
  echo "no Dockerfiles found under: $(tr '\n' ' ' < "$list")"
  : > built_images.env
  # built_submodules.tsv deliberately left as written above - a
  # submodule with no image can still be a git_stack whose pin moves.
  exit 0
fi

echo "images to consider:"
sed 's/^/  /' "$images"

# A registry-backed cache is the only kind that survives here: each job
# gets a throwaway dind whose local layer store dies with it, so without
# this every pipeline rebuilds a Next.js image from zero. Needs the
# docker-container driver - buildx's default "docker" driver cannot
# export a registry cache at all - which is set up by the caller.
#
# Capability-checked rather than assumed: if buildx or its builder
# isn't there, this falls back to a plain build instead of failing, so
# a missing cache is slow rather than broken. That fallback is also the
# only path verified end to end against the former LXC runner.
use_buildx=0
if [ "${BUILD_CACHE:-true}" = "true" ] && "$docker" buildx inspect >/dev/null 2>&1; then
  use_buildx=1
  echo "layer cache: buildx registry cache"
else
  echo "layer cache: none (plain docker build)"
fi

# The whole point of rebuilding an unchanged Dockerfile is to pick up
# base-OS security fixes, and a layer cache defeats exactly that: the
# `apt-get upgrade`/`apk upgrade` RUN is byte-identical from one build
# to the next, so --cache-from restores it and the upgrade never runs.
# --pull doesn't save it either - node:20-trixie-slim's own tag can sit
# unrebuilt for months while Debian ships point releases underneath it.
# Feeding the date into an ARG the Dockerfiles reference moves that
# layer's cache key once a day, so a rebuild is a real rebuild while
# everything within the same day still hits cache.
#
# Caught by a Trivy scan of dashboard-api:4f6d43c: 15 fixable
# findings, every one of them a Debian 13.7 package the image had had
# no chance to install.
refresh="$(date -u +%Y-%m-%d)"
echo "cache-bust args: APT_REFRESH=$refresh APK_REFRESH=$refresh"

: > built_images.env
while read -r name ctx sub; do
  [ -n "$name" ] || continue
  rev="$(git rev-parse --short "HEAD:$sub")"
  tag="$HARBOR_URL/$HARBOR_PROJECT/$name:$rev"

  # Tags are the submodule's own commit SHA, so this is a real question,
  # not just an optimization: the same commit legitimately shows up as
  # "changed" more than once (a retried job, a re-run after an unrelated
  # fix) without the source having changed again.
  #
  # It is also what keeps a tag immutable. FORCE_REBUILD overwrites one,
  # which is sometimes exactly right (a base-image CVE fix rebuilds
  # identical source into a genuinely different image) but means a tag
  # someone already deployed now points at different bytes. Opt-in only,
  # and it says so in the log.
  if "$docker" manifest inspect "$tag" >/dev/null 2>&1; then
    if [ "${FORCE_REBUILD:-false}" = "true" ]; then
      echo "$name: $tag already in Harbor, REBUILDING ANYWAY (FORCE_REBUILD=true, this tag will point at new bytes)"
    else
      echo "$name: $tag already in Harbor, skipping build"
      echo "${name}_IMAGE=$tag" >> built_images.env
      continue
    fi
  fi

  echo "building $name from $ctx (submodule $sub @ $rev)"
  cache_ref="$HARBOR_URL/$HARBOR_PROJECT/$name:buildcache"
  if [ "$use_buildx" -eq 1 ]; then
    # --pull, always. A runner can cache base layers across jobs, so
    # without it a rebuild of an unchanged Dockerfile silently keeps
    # whatever `FROM debian:trixie` resolved to the first time. Nearly
    # every commit in this repo is base-image CVE work, which is exactly
    # the rebuild a stale base quietly undoes while the pipeline still
    # reports a fresh tag pushed.
    #
    # mode=max caches intermediate stages too, not just the final one.
    # These are multi-stage builds whose expensive half (deps, the
    # Next.js compile) never reaches the runtime image.
    "$docker" buildx build --pull --push \
      --build-arg "APT_REFRESH=$refresh" --build-arg "APK_REFRESH=$refresh" \
      --cache-from "type=registry,ref=$cache_ref" \
      --cache-to "type=registry,ref=$cache_ref,mode=max,image-manifest=true" \
      -f "$ctx/Dockerfile" -t "$tag" "$ctx"
  else
    "$docker" build --pull \
      --build-arg "APT_REFRESH=$refresh" --build-arg "APK_REFRESH=$refresh" \
      -f "$ctx/Dockerfile" -t "$tag" "$ctx"
    "$docker" push "$tag"
  fi
  echo "${name}_IMAGE=$tag" >> built_images.env
done < "$images"

echo "built_images.env:"
sed 's/^/  /' built_images.env
