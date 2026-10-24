#!/usr/bin/env python3
"""Check every compose/*/docker-compose.yml for images with a newer
stable tag available upstream, then either open a merge request with the
bump applied, or file a GitLab issue, one per stack, depending on how
the image is pinned.

Run by the scheduled `check-image-updates` CI job (see .gitlab-ci.yml).
Safe to run locally too: with no GitLab CI variables in the environment
it just prints the findings and skips talking to the GitLab API.

Deliberately conservative:
  - only images with a version-looking current tag are checked at all
    (commit-hash tags, "latest", "release-x-tag" etc. have no ordering
    we can reason about and are skipped rather than guessed at)
  - a digest-pinned image is checked against the tag named in the
    comment above it (that's the only version string tying the digest
    to a release), not against the digest itself
  - a tag containing an alpha/beta/rc/dev/nightly/canary marker is
    never proposed as an upgrade target, even from another pre-release
  - a tag naming an OS or CPU architecture (windows, linux, arm64,
    amd64, ...) is never proposed either - some repos (portainer-ce)
    tag single-platform variants of a release alongside the multi-arch
    tag itself, and a platform name's digits (a Windows build year, an
    arch's own number) would otherwise out-rank the real version
  - only Docker Hub and ghcr.io are queried; anything else (private
    registries like registry.example.com, or a private ghcr.io image) is
    skipped rather than guessed at

Nothing is dropped silently: every image line it does not check is
printed under `not checked` with the reason. An image this script never
queried used to print nothing at all, which reads the same as a healthy
one - that is how all six images of one compose stack fell out of the check.

A plain tag-pinned image (`image: repo:1.2.3`) is routine to bump, so
it gets a merge request with the tag already swapped - merging it is
the whole action (and, like any other compose edit, immediately
redeploys that stack via the existing auto-deploy job). A digest-pinned
image (`image: repo@sha256:...`) was pinned that way on purpose, usually
with a comment explaining it isn't a routine bump (a dev build, a
migration risk) - those only ever get an issue, never a one-click MR,
so the deliberate step the comment calls for can't be skipped by
accident.

A bump MR whose every bump stays within the same major version (patch
or minor, see bump_level) is also set to merge when its pipeline
succeeds, so routine bumps reach production via auto-deploy without a
manual merge. An MR with any major bump keeps waiting for a human.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = REPO_ROOT / "compose"
LABEL = "image-update"

IMAGE_LINE_RE = re.compile(r'^\s*image:\s*([^\s#]+)')
COMMENTED_IMAGE_RE = re.compile(r'#.*\bimage:\s*([^\s#]+)')

# Escape hatch for a repo whose tagging scheme this script can't reason
# about safely (see the mysql example in the module docstring): put
#   # image-check: ignore
# on the image line itself, or anywhere in the comment block directly
# above it, and this image is skipped entirely.
IGNORE_RE = re.compile(r'#.*image-check:\s*ignore', re.IGNORECASE)

# A tag has to start with a version-looking token to be considered at
# all: v?1[.2[.3[.4]]] optionally followed by a suffix (e.g.
# "19.3.1-ce.0", "v2.14.0"). Rejects commit hashes, "release-x-tag",
# branch names - those have no ordering we can safely reason about.
VERSION_RE = re.compile(r'^v?(\d+(?:\.\d+){0,3})(.*)$')

# Present anywhere in the suffix, case-insensitive, marks a pre-release
# channel. Such a tag is never proposed as an upgrade target.
PRERELEASE_MARKERS = (
    "alpha", "beta", "rc", "dev", "nightly", "pre", "canary",
    "next", "edge", "unstable", "insider", "preview", "experimental",
)

# Same idea, for a single-platform variant of an otherwise multi-arch
# tag (e.g. portainer/portainer-ce publishes "2.45.0" as a multi-arch
# index AND "2.45.0-linux-amd64", "2.45.0-windowsltsc2025-amd64", ... as
# single-platform tags of that SAME release, all in one flat tag list).
# version_key() below turns digits anywhere in the suffix into part of
# the comparison, so an OS build number or an arch name containing
# digits ("windowsltsc2025", "amd64") can outrank the real version -
# that is exactly what proposed portainer-ce:2.45.0-windowsltsc2025-amd64
# over plain 2.45.0 on 2026-09-10, and it isn't even a linux image.
# Rejecting any suffix that names a platform keeps this script choosing
# the one multi-arch tag every other pin here already relies on.
PLATFORM_MARKERS = (
    "linux", "windows", "nanoserver", "ltsc", "darwin",
    "amd64", "arm64", "arm", "armhf", "armv7", "aarch64",
    "386", "ppc64le", "s390x", "riscv64", "x86_64",
)

# Bump levels an MR may auto-merge at. An MR auto-merges only when every
# bump in it is one of these; a single major bump holds the whole MR
# for review.
AUTO_MERGE_LEVELS = ("patch", "minor")

# How long to wait for GitLab to create the pipeline of a freshly pushed
# bump branch before giving up on auto-merge for this run. Auto-merge
# needs a head pipeline to attach to; if it isn't there yet, the next
# daily run picks the MR up again.
PIPELINE_WAIT_SECONDS = 60

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "deploy-image-update-check/1.0"


@dataclass
class ImageRef:
    stack: str
    file: Path
    line: int
    registry: str  # e.g. "docker.io", "ghcr.io", "registry.example.com"
    repo: str  # e.g. "example/app", "library/mysql"
    current_tag: str
    pinned_by_digest: bool
    # The exact "repo:tag" substring as written on that line, e.g.
    # "ghost:6.45.0" - None for a digest-pinned image, which is never
    # auto-bumped. Kept verbatim (not reconstructed from repo/current_tag)
    # so the line-edit in sync_mrs is an exact substring replace.
    raw_image: str | None


@dataclass
class Skipped:
    """An image line this run did not check, and why. Printed rather than
    dropped: in a silent run an unchecked image is indistinguishable from
    an up-to-date one, and a stack can fall out of the check entirely
    without anyone noticing (one stack did, for six images)."""
    stack: str
    line: int
    image: str  # as written on the line, or "registry/repo:tag" once parsed
    reason: str

    def display(self) -> str:
        repo, sep, digest = self.image.partition("@sha256:")
        return f"{repo}@sha256:{digest[:12]}..." if sep else self.image


@dataclass
class Outdated:
    ref: ImageRef
    latest_tag: str

    @property
    def level(self) -> str:
        return bump_level(self.ref.current_tag, self.latest_tag)


def parse_version(tag: str):
    """Return (core, suffix_numbers) for a version-looking tag, or None
    if it isn't one (or is a pre-release, or a single-platform variant)."""
    m = VERSION_RE.match(tag)
    if not m:
        return None
    suffix = m.group(2)
    suffix_lower = suffix.lower()
    if any(marker in suffix_lower for marker in PRERELEASE_MARKERS):
        return None
    if any(marker in suffix_lower for marker in PLATFORM_MARKERS):
        return None
    core = tuple(int(x) for x in m.group(1).split("."))
    suffix_nums = tuple(int(x) for x in re.findall(r"\d+", suffix))
    return core, suffix_nums


def bump_level(current_tag: str, latest_tag: str) -> str:
    """"major", "minor" or "patch" for a bump between two tags that
    latest_stable_tag already found comparable. Only a change in the
    first component is major, a change in the second is minor, anything
    else (third/fourth component, or just the suffix) is patch. A
    single-component tag ("8" -> "9") can only ever be a major bump."""
    cur, new = parse_version(current_tag), parse_version(latest_tag)
    if cur is None or new is None or cur[0][0] != new[0][0]:
        return "major"
    if len(cur[0]) > 1 and cur[0][1] != new[0][1]:
        return "minor"
    return "patch"


def version_key(parsed, width: int):
    core, suffix_nums = parsed
    return core + (0,) * (width - len(core)), suffix_nums


def split_registry(repo_path: str):
    first, sep, rest = repo_path.partition("/")
    if sep and ("." in first or ":" in first):
        return first, rest
    if not sep:
        return "docker.io", f"library/{repo_path}"
    return "docker.io", repo_path


def split_tag(repo_path: str) -> tuple[str, str | None]:
    """Split a trailing ":tag" off an image path: a colon names a tag only
    when what follows it isn't a path component, so a registry port
    (registry.example.com:5000/x) doesn't read as one."""
    repo, sep, tag = repo_path.rpartition(":")
    if not sep or "/" in tag:
        return repo_path, None
    return repo, tag


def comment_block_above(lines: list[str], i: int, max_lookback: int = 25) -> list[str]:
    """Comment lines contiguous with (and directly above) line i, i.e.
    the hand-written note explaining that image pin."""
    block = []
    for j in range(i - 1, max(i - 1 - max_lookback, -1), -1):
        stripped = lines[j].strip()
        if not stripped.startswith("#"):
            if stripped:
                break  # hit real code above, stop looking
            continue
        block.append(lines[j])
    return block


def find_images() -> tuple[list[ImageRef], list[Skipped]]:
    refs: list[ImageRef] = []
    skips: list[Skipped] = []
    for compose_file in sorted(COMPOSE_DIR.glob("*/docker-compose.yml")):
        stack = compose_file.parent.name
        lines = compose_file.read_text().splitlines()
        for i, line in enumerate(lines):
            m = IMAGE_LINE_RE.match(line)
            if not m:
                continue
            image = m.group(1).strip("\"'")
            block = comment_block_above(lines, i)

            if IGNORE_RE.search(line) or any(IGNORE_RE.search(c) for c in block):
                skips.append(Skipped(stack, i + 1, image, "image-check: ignore"))
                continue

            pinned_by_digest = "@sha256:" in image
            raw_image = None
            if pinned_by_digest:
                # Two places a digest pin can still name its version: in the
                # ref itself (repo:TAG@sha256:...), or in the comment block
                # above it. The ref wins - it is the one the deploy verifies.
                repo_path, tag = split_tag(image.split("@sha256:")[0])
                if tag is None:
                    for c in block:
                        hint = COMMENTED_IMAGE_RE.search(c)
                        if hint and ":" in hint.group(1):
                            _, _, tag = hint.group(1).rpartition(":")
                            break
                if tag is None:
                    skips.append(Skipped(
                        stack, i + 1, image,
                        "digest pin with no version hint - write the tag into the ref "
                        "(repo:TAG@sha256:...) or name it in a comment above",
                    ))
                    continue
            else:
                repo_path, tag = split_tag(image)
                if tag is None:
                    skips.append(Skipped(stack, i + 1, image, "no tag on the ref"))
                    continue
                raw_image = image

            if tag in ("latest", "stable"):
                skips.append(Skipped(stack, i + 1, image, f"floating tag {tag}"))
                continue

            registry, repo = split_registry(repo_path)
            refs.append(ImageRef(stack, compose_file, i + 1, registry, repo, tag, pinned_by_digest, raw_image))
    return refs, skips


def fetch_dockerhub_tags(repo: str, max_pages: int = 5) -> list[str] | None:
    tags: list[str] = []
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags"
    params = {"page_size": 100, "ordering": "last_updated"}
    for _ in range(max_pages):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            print(f"  ! docker hub request failed for {repo}: {e}", file=sys.stderr)
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            print(f"  ! docker hub returned {resp.status_code} for {repo}", file=sys.stderr)
            return None
        data = resp.json()
        tags.extend(r["name"] for r in data.get("results", []))
        url, params = data.get("next"), None  # `next` already carries the query string
        if not url:
            break
    return tags


def fetch_ghcr_tags(repo: str, max_pages: int = 20) -> list[str] | None:
    """Every tag in the repo, following the registry's pagination.

    This MUST page. /v2/<repo>/tags/list returns one page and points at
    the next through a Link header, and the order is push order, not
    version order - so a repo with a few hundred tags (one stack's six have
    200+ each, most of them sha-<commit>) answers a bare request with its
    OLDEST tags and nothing else. Read only that page and every one of
    those images looks pinned at its newest version when it is releases
    behind, which is exactly what this script reported until 2026-09-15.
    """
    try:
        token_resp = SESSION.get(
            "https://ghcr.io/token",
            params={"scope": f"repository:{repo}:pull"},
            timeout=15,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        tags: list[str] = []
        url, params = f"https://ghcr.io/v2/{repo}/tags/list", {"n": 1000}
        for _ in range(max_pages):
            resp = SESSION.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code == 401:
                print(f"  ~ ghcr.io/{repo} is private (no anonymous pull) - skipping", file=sys.stderr)
                return None
            resp.raise_for_status()
            tags.extend(resp.json().get("tags", []))
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                return tags
            # The Link header is relative (/v2/...), and already carries
            # its own `n`/`last` query string.
            url, params = requests.compat.urljoin("https://ghcr.io", nxt), None
        print(f"  ! ghcr.io/{repo} has more than {max_pages} pages of tags - list truncated", file=sys.stderr)
        return tags
    except requests.RequestException as e:
        print(f"  ! ghcr.io request failed for {repo}: {e}", file=sys.stderr)
        return None


def latest_stable_tag(ref: ImageRef) -> tuple[str | None, str | None]:
    """(newer tag, None) when an upgrade is available, (None, None) when the
    pin is already the newest on its track, (None, reason) when this image
    could not be checked at all."""
    current = parse_version(ref.current_tag)
    if current is None:
        return None, f"tag {ref.current_tag} has no version ordering to compare against"

    if ref.registry == "docker.io":
        tags = fetch_dockerhub_tags(ref.repo)
    elif ref.registry == "ghcr.io":
        tags = fetch_ghcr_tags(ref.repo)
    else:
        return None, f"{ref.registry} is not queried (private/unsupported registry)"

    if not tags:
        return None, "no tag list available from the registry"

    # Restrict to tags with the same number of dot-separated components as
    # the current one (e.g. "8.0" only compares against other X.Y tags).
    # Docker Hub's official images publish several concurrent numbering
    # tracks under one repo - e.g. mysql ships "8.4" (LTS), "9.7"
    # (innovation) and "26.7.0" side by side - and without this a naive
    # "biggest number wins" comparison picks whichever track happens to
    # have the largest leading digit, not an actual upgrade path.
    width = len(current[0])
    parsed_tags = [(t, parse_version(t)) for t in tags]
    parsed_tags = [(t, p) for t, p in parsed_tags if p is not None and len(p[0]) == width]
    if not parsed_tags:
        return None, f"registry publishes no comparable {width}-component tags"

    current_key = version_key(current, width)
    best_tag, best_key = None, current_key
    for tag, parsed in parsed_tags:
        key = version_key(parsed, width)
        if key > best_key or (key == best_key and best_tag is not None and len(tag) < len(best_tag)):
            best_key, best_tag = key, tag
    return best_tag, None


def check_all() -> list[Outdated]:
    refs, skips = find_images()
    outdated: list[Outdated] = []
    current = 0
    for ref in refs:
        label = f"{ref.registry}/{ref.repo}"
        latest, reason = latest_stable_tag(ref)
        if latest:
            print(f"  UPDATE  {ref.stack:<12} {label}:{ref.current_tag} -> {latest}")
            outdated.append(Outdated(ref, latest))
        elif reason:
            skips.append(Skipped(ref.stack, ref.line, f"{label}:{ref.current_tag}", reason))
        else:
            print(f"  ok      {ref.stack:<12} {label}:{ref.current_tag}")
            current += 1

    if skips:
        print(f"\n  not checked ({len(skips)}):")
        for s in sorted(skips, key=lambda s: (s.stack, s.line)):
            print(f"  skip    {s.stack:<12} {s.display()}")
            print(f"          {'':<12} {s.reason}")
    print(f"\n  {len(outdated)} update(s), {current} current, {len(skips)} not checked\n")
    return outdated


def gitlab_api():
    """(api_url, project_id, headers), or None if there's nothing to auth
    with. Prefers GITLAB_API_TOKEN (a project access token, PRIVATE-TOKEN
    header): this GitLab instance returns 401 for CI_JOB_TOKEN against
    the Issues API, so the job token is only a fallback here in case a
    future instance/version does support it."""
    api_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_PROJECT_ID")
    if not (api_url and project_id):
        return None
    if os.environ.get("GITLAB_API_TOKEN"):
        return api_url, project_id, {"PRIVATE-TOKEN": os.environ["GITLAB_API_TOKEN"]}
    if os.environ.get("CI_JOB_TOKEN"):
        return api_url, project_id, {"JOB-TOKEN": os.environ["CI_JOB_TOKEN"]}
    return None


def issue_body(stack: str, items: list[Outdated]) -> str:
    lines = [
        f"Automated check found {len(items)} image update(s) available for the "
        f"`{stack}` stack. Review and bump `compose/{stack}/docker-compose.yml` "
        "deliberately, don't just chase `latest`.",
        "",
    ]
    for o in items:
        r = o.ref
        where = f"`{r.file.relative_to(REPO_ROOT)}:{r.line}`"
        if r.pinned_by_digest:
            lines.append(
                f"- **{r.registry}/{r.repo}**: pinned by digest, tracked tag "
                f"`{r.current_tag}` -> `{o.latest_tag}` available ({where})"
            )
        else:
            lines.append(f"- **{r.registry}/{r.repo}**: `{r.current_tag}` -> `{o.latest_tag}` available ({where})")
    lines.append("")
    lines.append(
        f"_Filed automatically by the `check-image-updates` schedule "
        f"({os.environ.get('CI_PIPELINE_URL', 'local run')})._"
    )
    return "\n".join(lines)


def sync_issues(by_stack: dict[str, list[Outdated]]) -> None:
    creds = gitlab_api()
    if creds is None:
        print("no GitLab API credentials in the environment - printing results only.")
        return
    api_url, project_id, headers = creds
    base = f"{api_url}/projects/{project_id}/issues"

    open_issues = []
    url, params = base, {"state": "opened", "labels": LABEL, "per_page": 100}
    while url:
        resp = SESSION.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        open_issues.extend(resp.json())
        url, params = resp.links.get("next", {}).get("url"), None

    by_stack_existing = {}
    for issue in open_issues:
        m = re.match(r"^([\w.-]+): \d+ image update", issue["title"])
        if m:
            by_stack_existing[m.group(1)] = issue

    for stack, items in by_stack.items():
        title = f"{stack}: {len(items)} image update{'s' if len(items) != 1 else ''} available"
        body = issue_body(stack, items)
        existing = by_stack_existing.pop(stack, None)
        if existing:
            resp = SESSION.put(
                f"{base}/{existing['iid']}",
                headers=headers,
                data={"title": title, "description": body},
                timeout=15,
            )
            resp.raise_for_status()
            print(f"  updated issue !{existing['iid']} for {stack}")
        else:
            resp = SESSION.post(
                base,
                headers=headers,
                data={"title": title, "description": body, "labels": LABEL},
                timeout=15,
            )
            resp.raise_for_status()
            print(f"  filed issue !{resp.json()['iid']} for {stack}")

    # Any stack that still has an open issue but no outdated images left
    # this run has caught up - close it rather than leaving it stale.
    for stack, issue in by_stack_existing.items():
        resp = SESSION.put(
            f"{base}/{issue['iid']}",
            headers=headers,
            data={"state_event": "close"},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  closed issue !{issue['iid']} for {stack} (no longer outdated)")


def get_file_at_ref(base: str, headers: dict, path: str, ref: str) -> str | None:
    """Raw content of `path` at `ref`, or None if the file/branch doesn't
    exist there yet."""
    encoded = requests.utils.quote(path, safe="")
    resp = SESSION.get(f"{base}/repository/files/{encoded}/raw", headers=headers, params={"ref": ref}, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def push_bump_commit(base: str, headers: dict, branch: str, path: str, content: str, message: str) -> bool:
    """Commit `content` for `path` on `branch` (creating the branch from
    main if it doesn't exist yet). Returns False without committing if
    the branch already has this exact content, so a re-run that found
    nothing new doesn't produce an empty commit."""
    if get_file_at_ref(base, headers, path, branch) == content:
        return False
    resp = SESSION.post(
        f"{base}/repository/commits",
        headers=headers,
        json={
            "branch": branch,
            "start_branch": "main",
            "commit_message": message,
            "actions": [{"action": "update", "file_path": path, "content": content}],
        },
        timeout=20,
    )
    resp.raise_for_status()
    return True


def find_open_bump_mrs(base: str, headers: dict) -> dict[str, dict]:
    """stack -> MR, for open MRs whose source branch is image-update/<stack>."""
    mrs: dict[str, dict] = {}
    url, params = f"{base}/merge_requests", {"state": "opened", "labels": LABEL, "per_page": 100}
    while url:
        resp = SESSION.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        for mr in resp.json():
            m = re.match(r"^image-update/([\w.-]+)$", mr["source_branch"])
            if m:
                mrs[m.group(1)] = mr
        url, params = resp.links.get("next", {}).get("url"), None
    return mrs


def mr_title(stack: str, items: list[Outdated]) -> str:
    return f"{stack}: bump {len(items)} image{'s' if len(items) != 1 else ''}"


def auto_mergeable(items: list[Outdated]) -> bool:
    return all(o.level in AUTO_MERGE_LEVELS for o in items)


def mr_body(stack: str, items: list[Outdated]) -> str:
    if auto_mergeable(items):
        note = (
            f"**Queued for auto-merge**: every bump is {' or '.join(AUTO_MERGE_LEVELS)}, "
            f"so this merges when its pipeline succeeds and then redeploys `{stack}` "
            "via auto-deploy. Close it to stop that."
        )
    else:
        note = (
            f"**Needs review**: includes a major bump. Merging this redeploys `{stack}` "
            "immediately (same as any other compose change on main), review the diff first."
        )
    lines = [
        f"Automated bump for the `{stack}` stack: {len(items)} image(s) to the latest "
        "stable tag on the same numbering track.",
        "",
        note,
        "",
    ]
    for o in items:
        r = o.ref
        where = f"`{r.file.relative_to(REPO_ROOT)}:{r.line}`"
        lines.append(f"- **{r.registry}/{r.repo}**: `{r.current_tag}` -> `{o.latest_tag}`, {o.level} ({where})")
    lines.append("")
    lines.append(
        f"_Opened automatically by the `check-image-updates` schedule "
        f"({os.environ.get('CI_PIPELINE_URL', 'local run')})._"
    )
    return "\n".join(lines)


def get_mr(base: str, headers: dict, iid: int) -> dict:
    resp = SESSION.get(f"{base}/merge_requests/{iid}", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def wait_for_head_pipeline(base: str, headers: dict, iid: int) -> dict:
    """The MR, re-fetched until it has a head pipeline or
    PIPELINE_WAIT_SECONDS runs out (GitLab creates it asynchronously
    after the push)."""
    deadline = time.monotonic() + PIPELINE_WAIT_SECONDS
    while True:
        mr = get_mr(base, headers, iid)
        if mr.get("head_pipeline") or time.monotonic() >= deadline:
            return mr
        time.sleep(5)


def sync_auto_merge(base: str, headers: dict, stack: str, iid: int, wanted: bool) -> None:
    """Set (or cancel) merge-when-pipeline-succeeds on a bump MR. Never
    raises on a refused merge: that leaves the MR open for a human,
    which is the safe outcome, and must not stop the rest of the run."""
    mr = wait_for_head_pipeline(base, headers, iid) if wanted else get_mr(base, headers, iid)
    queued = mr.get("merge_when_pipeline_succeeds")

    if not wanted:
        if queued:
            resp = SESSION.post(
                f"{base}/merge_requests/{iid}/cancel_merge_when_pipeline_succeeds",
                headers=headers,
                timeout=15,
            )
            if resp.ok:
                print(f"  cancelled auto-merge on MR !{iid} for {stack} (now has a major bump)")
            else:
                print(f"  ! {stack}: cancelling auto-merge on MR !{iid} returned {resp.status_code}", file=sys.stderr)
        return

    if queued:
        print(f"  MR !{iid} for {stack} already queued for auto-merge")
        return
    pipeline = mr.get("head_pipeline")
    if not pipeline:
        print(f"  MR !{iid} for {stack} has no pipeline yet, auto-merge retried next run")
        return
    if pipeline.get("status") in ("failed", "canceled", "skipped"):
        print(f"  ! {stack}: MR !{iid} pipeline {pipeline['status']}, left for review", file=sys.stderr)
        return

    resp = SESSION.put(
        f"{base}/merge_requests/{iid}/merge",
        headers=headers,
        data={
            # `sha` makes GitLab refuse if the branch moved since we
            # looked, so only the bump checked here can merge.
            "sha": mr["sha"],
            "merge_when_pipeline_succeeds": True,
            "auto_merge": True,
            "should_remove_source_branch": True,
        },
        timeout=15,
    )
    if resp.ok:
        state = "merged" if resp.json().get("state") == "merged" else "queued for auto-merge"
        print(f"  MR !{iid} for {stack} {state}")
    else:
        # 401/403: GITLAB_API_TOKEN can't merge into protected main.
        # 405/406/409/422: not mergeable right now (conflict, moved, ...).
        print(f"  ! {stack}: auto-merge on MR !{iid} returned {resp.status_code}: {resp.text[:200]}", file=sys.stderr)


def sync_mrs(by_stack: dict[str, list[Outdated]]) -> None:
    creds = gitlab_api()
    if creds is None:
        print("no GitLab API credentials in the environment - not opening MRs.")
        return
    api_url, project_id, headers = creds
    base = f"{api_url}/projects/{project_id}"

    open_mrs = find_open_bump_mrs(base, headers)

    for stack, items in by_stack.items():
        rel_path = str(items[0].ref.file.relative_to(REPO_ROOT))
        lines = items[0].ref.file.read_text().splitlines(keepends=True)
        drifted = False
        for o in items:
            r = o.ref
            idx = r.line - 1
            if r.raw_image not in lines[idx]:
                print(f"  ! {stack}: {r.file.name}:{r.line} no longer matches the scanned image, skipping MR", file=sys.stderr)
                drifted = True
                break
            new_image = f"{r.raw_image.rsplit(':', 1)[0]}:{o.latest_tag}"
            lines[idx] = lines[idx].replace(r.raw_image, new_image, 1)
        if drifted:
            continue

        branch = f"image-update/{stack}"
        title = mr_title(stack, items)
        changed = push_bump_commit(base, headers, branch, rel_path, "".join(lines), title)

        existing = open_mrs.pop(stack, None)
        if existing:
            iid = existing["iid"]
            if changed:
                resp = SESSION.put(
                    f"{base}/merge_requests/{existing['iid']}",
                    headers=headers,
                    data={"title": title, "description": mr_body(stack, items)},
                    timeout=15,
                )
                resp.raise_for_status()
                print(f"  updated MR !{existing['iid']} for {stack}")
            else:
                print(f"  MR !{existing['iid']} for {stack} already up to date")
        else:
            resp = SESSION.post(
                f"{base}/merge_requests",
                headers=headers,
                data={
                    "source_branch": branch,
                    "target_branch": "main",
                    "title": title,
                    "description": mr_body(stack, items),
                    "labels": LABEL,
                    "remove_source_branch": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            iid = resp.json()["iid"]
            print(f"  opened MR !{iid} for {stack}")

        sync_auto_merge(base, headers, stack, iid, auto_mergeable(items))

    # Anything still open here has no outdated tag-pinned images left -
    # this run has caught up (or someone bumped it by hand) - so close
    # it rather than leaving a stale MR around. The branch itself is
    # left in place; GitLab doesn't delete it on a manual close.
    for stack, mr in open_mrs.items():
        resp = SESSION.put(
            f"{base}/merge_requests/{mr['iid']}",
            headers=headers,
            data={"state_event": "close"},
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  closed MR !{mr['iid']} for {stack} (no longer outdated)")


def main() -> None:
    outdated = check_all()
    mr_by_stack: dict[str, list[Outdated]] = {}
    issue_by_stack: dict[str, list[Outdated]] = {}
    for o in outdated:
        target = issue_by_stack if o.ref.pinned_by_digest else mr_by_stack
        target.setdefault(o.ref.stack, []).append(o)
    sync_mrs(mr_by_stack)
    sync_issues(issue_by_stack)


if __name__ == "__main__":
    main()
