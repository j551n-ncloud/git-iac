#!/usr/bin/env python3
"""Move services/deploy forward to what build-and-push just produced,
via a merge request - never a direct commit, so a human reviews before
anything redeploys.

Two things get bumped, from two artifacts:

  IMAGE TAGS, from built_images.env - the `image:` line in whichever
  compose file already references that Harbor repo.

  GIT STACK PINS, from built_submodules.tsv - the `version:` of the
  git_stacks entry whose `repo:` is that submodule's remote. That pin
  governs the checkout (bind-mounted config), not the images.

Conservative in both: it only ever edits something that ALREADY points
at this artifact, never creates a stack, never guesses. No match means
it says so and skips.
"""
from __future__ import annotations

import os
import re
import sys

import requests
import yaml

LABEL = "build-update"
SESSION = requests.Session()


def load_built_images(path: str = "built_images.env") -> dict[str, str]:
    """name -> full image ref (harbor/project/name:tag), keyed by the
    submodule directory name (NAME_IMAGE=... -> NAME)."""
    images: dict[str, str] = {}
    if not os.path.exists(path):
        return images
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.endswith("_IMAGE"):
                images[key[: -len("_IMAGE")]] = value
    return images


def load_built_submodules(path: str = "built_submodules.tsv") -> list[tuple[str, str, str]]:
    """(submodule path, full commit SHA, remote URL) per submodule that
    moved this run. Written by build-images.sh."""
    rows: list[tuple[str, str, str]] = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3 and parts[0] and parts[1]:
                rows.append((parts[0], parts[1], parts[2]))
    return rows


def normalize_repo(url: str) -> str:
    """Compare git remotes by host+path, so the SSH and HTTPS spellings
    of one repo match. Without it .gitmodules and deploy's `repo:` can
    disagree on form and the pin silently never gets bumped."""
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u)          # https://, ssh://, git://
    u = re.sub(r"^[^/@]+@", "", u)             # git@, user@
    u = u.replace(":", "/", 1) if "/" not in u.split(":", 1)[0] else u
    u = re.sub(r"\.git$", "", u)
    return u.strip("/").lower()


class _TolerantLoader(yaml.SafeLoader):
    """host_vars can carry !vault (and other) tags. This script only
    reads these files to FIND a matching stack - the edit itself is a
    line-level text change - so an unknown tag just needs to not raise."""


_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def find_git_stack(content: str, repo_url: str) -> tuple[str, str] | None:
    """(stack name, currently pinned version) for the git_stacks entry
    whose repo: is this submodule's remote, or None."""
    try:
        data = yaml.load(content, Loader=_TolerantLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    wanted = normalize_repo(repo_url)
    for stack in data.get("git_stacks") or []:
        if not isinstance(stack, dict):
            continue
        if stack.get("repo") and normalize_repo(str(stack["repo"])) == wanted:
            return str(stack.get("name", "")), str(stack.get("version", ""))
    return None


def bump_version_line(content: str, stack_name: str, new_sha: str) -> str | None:
    """Replace the `version:` of the named git_stacks entry.

    A line edit, not a YAML round-trip: a dump would drop every comment
    in host_vars and bury the one-line bump a reviewer is here to read.
    None if it can't be located, which the caller treats as a skip."""
    lines = content.splitlines(keepends=True)
    start = None
    indent = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-(\s+)name:\s*['\"]?" + re.escape(stack_name) + r"['\"]?\s*$", line)
        if m:
            start = i
            indent = len(m.group(1))
            break
    if start is None:
        return None

    # The entry runs to the next list item at the same indent, or to the
    # first line that dedents out of the list entirely.
    for j in range(start + 1, len(lines) + 1):
        if j == len(lines):
            break
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent:
            break
        m = re.match(r"^(\s*version:\s*)\S+(\s*)$", line)
        if m:
            lines[j] = f"{m.group(1)}{new_sha}\n"
            return "".join(lines)
    return None


def deploy_api() -> tuple[str, str, dict] | None:
    """(api_url, project_path, headers) for services/deploy - a
    different project than this pipeline's own CI_PROJECT_ID, so this
    can't reuse CI_API_V4_URL's project-scoped shortcuts, only the
    instance's base API URL."""
    api_url = os.environ.get("CI_API_V4_URL")
    token = os.environ.get("DEPLOY_API_TOKEN")
    project_path = os.environ.get("DEPLOY_PROJECT_PATH", "services/deploy")
    # Hard failure, not a skip. Images were built and pushed to Harbor by
    # the time this runs, so "no MR opened" is a real outcome someone has
    # to act on - and a green pipeline that quietly opened nothing looks
    # exactly like one that had nothing to open. services/deploy refuses
    # the same trade in .ansible_vault and .proxmox_creds.
    if not token:
        print("DEPLOY_API_TOKEN is empty - cannot open the compose bump MR.", file=sys.stderr)
        print("  Set it in Settings > CI/CD > Variables (see .gitlab-ci.yml's header),", file=sys.stderr)
        print("  and check its scope: protected variables are absent on unprotected branches.", file=sys.stderr)
        sys.exit(1)
    if not api_url:
        print("CI_API_V4_URL is empty - this script expects to run inside CI.", file=sys.stderr)
        sys.exit(1)
    encoded = requests.utils.quote(project_path, safe="")
    return api_url, encoded, {"PRIVATE-TOKEN": token}


def get_file_at_ref(base: str, headers: dict, path: str, ref: str) -> str | None:
    encoded = requests.utils.quote(path, safe="")
    resp = SESSION.get(f"{base}/repository/files/{encoded}/raw", headers=headers, params={"ref": ref}, timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def list_host_vars(base: str, headers: dict) -> list[str]:
    """Every inventory/host_vars/*.yml in deploy on main. Listed rather
    than assumed, because which host a git stack lives on is exactly the
    thing this script must not hardcode."""
    resp = SESSION.get(
        f"{base}/repository/tree",
        headers=headers,
        params={"path": "inventory/host_vars", "ref": "main", "per_page": 100},
        timeout=15,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return [e["path"] for e in resp.json()
            if e["type"] == "blob" and e["path"].endswith((".yml", ".yaml"))]


def list_compose_files(base: str, headers: dict) -> list[str]:
    """Every compose/*/docker-compose.yml in deploy on main."""
    resp = SESSION.get(
        f"{base}/repository/tree",
        headers=headers,
        params={"path": "compose", "ref": "main", "recursive": "true", "per_page": 100},
        timeout=15,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return sorted(e["path"] for e in resp.json()
                  if e["type"] == "blob" and e["path"].endswith("/docker-compose.yml"))


def find_compose_path(base: str, headers: dict, harbor_repo: str,
                      cache: dict[str, str | None]) -> str | None:
    """The compose file that already references this Harbor repo, or
    None.

    Searched by content, not by assuming compose/<image-name>/: a stack
    can hold several images, and then the directory is named after the
    stack (dashboard has two, in compose/dashboard/)."""
    for path in list_compose_files(base, headers):
        if path not in cache:
            cache[path] = get_file_at_ref(base, headers, path, "main")
        content = cache[path]
        if content and bump_image_line(content, harbor_repo, "probe") is not None:
            return path
    return None


def bump_image_line(content: str, harbor_repo: str, new_ref: str) -> str | None:
    """Replace an `image: <harbor_repo>:<anything>` line with new_ref.
    Returns None if no line in this file actually references that
    Harbor repo - the caller treats that as "not this stack's image,
    skip it" rather than a bug."""
    pattern = re.compile(r"^(\s*image:\s*)" + re.escape(harbor_repo) + r":\S+", re.MULTILINE)
    new_content, count = pattern.subn(lambda m: m.group(1) + new_ref, content)
    return new_content if count else None


def branch_exists(base: str, headers: dict, branch: str) -> bool:
    encoded = requests.utils.quote(branch, safe="")
    resp = SESSION.get(f"{base}/repository/branches/{encoded}", headers=headers, timeout=15)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def delete_branch(base: str, headers: dict, branch: str) -> None:
    encoded = requests.utils.quote(branch, safe="")
    resp = SESSION.delete(f"{base}/repository/branches/{encoded}", headers=headers, timeout=15)
    if resp.status_code != 404:
        resp.raise_for_status()


def push_bump_commit(base: str, headers: dict, branch: str, files: dict[str, str],
                     message: str, reuse_branch: bool) -> bool:
    """Commit every file in `files` onto `branch` in ONE commit. False if
    the branch already holds exactly this, so a re-run doesn't make an
    empty commit on an MR someone is reviewing.

    reuse_branch says an open MR is using this branch, which decides the
    two things below.

    start_branch tells the Commits API to create `branch` fresh off main,
    and is a 400 against a branch that already exists - the bug commit
    09b2a49 fixed. But only checking existence, as that fix did, means a
    branch surviving a *closed* (not merged) MR gets built on instead:
    GitLab's remove_source_branch only fires on merge, so a closed MR
    leaves its branch behind, pointing at whatever main looked like back
    then. The bump commit itself would still be correct (the content
    written is derived from main as it is now, not from the branch), but
    the MR opened from it diffs against that stale merge base, so the
    reviewer is shown every unrelated change main made to the file since
    as though this MR introduced it. The only thing a human does here is
    read that diff, so it needs to show one line changing.

    No open MR owns such a branch, so nothing is lost by deleting it and
    cutting a fresh one from main."""
    if reuse_branch:
        if all(get_file_at_ref(base, headers, p, branch) == c for p, c in files.items()):
            return False
    elif branch_exists(base, headers, branch):
        print(f"  {branch} left over from a closed MR - recreating it from main")
        delete_branch(base, headers, branch)

    payload = {
        "branch": branch,
        "commit_message": message,
        "actions": [{"action": "update", "file_path": p, "content": c}
                    for p, c in sorted(files.items())],
    }
    if not reuse_branch:
        payload["start_branch"] = "main"
    resp = SESSION.post(f"{base}/repository/commits", headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return True


def find_existing_mr(base: str, headers: dict, branch: str) -> dict | None:
    resp = SESSION.get(
        f"{base}/merge_requests",
        headers=headers,
        params={"state": "opened", "source_branch": branch, "per_page": 1},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


def mr_body(stack: str, lines: list[str], paths: list[str]) -> str:
    return "\n".join([
        f"Automated bump for `{stack}`:",
        "",
        *(f"- {l}" for l in lines),
        "",
        "Files:",
        *(f"- `{p}`" for p in paths),
        "",
        f"**Merging this redeploys `{stack}` immediately** - review it first.",
        "",
        f"_Opened automatically by services/build "
        f"({os.environ.get('CI_PIPELINE_URL', 'local run')})._",
    ])


def open_or_update_mr(base: str, headers: dict, branch: str, files: dict[str, str],
                      title: str, body: str, label_name: str) -> None:
    """Commit onto the branch, then open the MR or update the one on it."""
    # Nothing to review, so nothing to open. push_bump_commit's own
    # equality check only runs when REUSING a branch, so a fresh branch
    # used to open MRs with zero files changed (deploy !15, !17).
    if all(get_file_at_ref(base, headers, p, "main") == c for p, c in files.items()):
        print(f"  {label_name}: services/deploy already at this version - no MR needed")
        return

    existing = find_existing_mr(base, headers, branch)
    changed = push_bump_commit(base, headers, branch, files, title,
                               reuse_branch=existing is not None)
    if existing:
        if changed:
            resp = SESSION.put(
                f"{base}/merge_requests/{existing['iid']}",
                headers=headers,
                data={"title": title, "description": body},
                timeout=15,
            )
            resp.raise_for_status()
            print(f"  updated MR !{existing['iid']} for {label_name}")
        else:
            print(f"  MR !{existing['iid']} for {label_name} already up to date")
        return
    resp = SESSION.post(
        f"{base}/merge_requests",
        headers=headers,
        data={
            "source_branch": branch,
            "target_branch": "main",
            "title": title,
            "description": body,
            "labels": LABEL,
            "remove_source_branch": True,
        },
        timeout=15,
    )
    resp.raise_for_status()
    print(f"  opened MR !{resp.json()['iid']} for {label_name}")


def stack_of(path: str) -> str:
    """Which stack a file belongs to. compose/<stack>/docker-compose.yml
    and a host_vars entry both resolve to the stack's own name, so the
    image bumps and the pin bump for one stack group together."""
    parts = path.split("/")
    return parts[1] if path.startswith("compose/") and len(parts) > 2 else ""


def collect_image_bumps(base: str, headers: dict) -> dict[str, tuple[str, str]]:
    """path -> (new content, one-line summary), for every built image
    that some compose file already pins."""
    out: dict[str, tuple[str, str]] = {}
    cache: dict[str, str | None] = {}
    for name, new_ref in load_built_images().items():
        harbor_repo = new_ref.rsplit(":", 1)[0]
        path = find_compose_path(base, headers, harbor_repo, cache)
        if path is None:
            print(f"  ! {name}: no compose file in services/deploy references "
                  f"{harbor_repo} - skipping, pin it there first.", file=sys.stderr)
            continue
        # Later images fold into the edit the earlier ones already made,
        # so two images in one file end up as one commit, not two that
        # each revert the other.
        base_content = out[path][0] if path in out else cache[path]
        new_content = bump_image_line(base_content, harbor_repo, new_ref)
        if new_content is None:
            print(f"  ! {name}: {path} has no image: line for {harbor_repo} - skipping.",
                  file=sys.stderr)
            continue
        tag = new_ref.rsplit(":", 1)[1]
        out[path] = (new_content, f"`{name}` -> `{tag}`")
    return out


def collect_pin_bumps(base: str, headers: dict) -> dict[str, tuple[str, str, str]]:
    """path -> (new content, summary, stack name), for every moved
    submodule that some host_vars git_stacks entry pins."""
    out: dict[str, tuple[str, str, str]] = {}
    submodules = load_built_submodules()
    if not submodules:
        return out
    host_vars = list_host_vars(base, headers)
    if not host_vars:
        print("  ! no inventory/host_vars/*.yml in services/deploy - no git stacks to bump.",
              file=sys.stderr)
        return out

    for sub, new_sha, url in submodules:
        if not url:
            print(f"  ! {sub}: no URL in .gitmodules - cannot match a git_stacks entry.",
                  file=sys.stderr)
            continue
        for path in host_vars:
            content = out[path][0] if path in out else get_file_at_ref(base, headers, path, "main")
            if content is None:
                continue
            found = find_git_stack(content, url)
            if not found:
                continue
            stack_name, old_sha = found
            if old_sha == new_sha:
                print(f"  {stack_name}: pin already at {new_sha[:7]}")
                break
            new_content = bump_version_line(content, stack_name, new_sha)
            if new_content is None:
                print(f"  ! {stack_name}: no version: line in {path} - skipping.",
                      file=sys.stderr)
                break
            out[path] = (new_content, f"checkout pin -> `{new_sha[:7]}`", stack_name)
            break
    return out


def main() -> None:
    if not load_built_images() and not load_built_submodules():
        print("nothing built this run - nothing to do.")
        return

    creds = deploy_api()
    if creds is None:
        return
    api_url, project_path, headers = creds
    base = f"{api_url}/projects/{project_path}"

    # Group every edit by the stack it belongs to, so one change to one
    # stack is ONE merge request however many files it touches. Per
    # artifact, dashboard produced three: two images and a pin, on three
    # branches, two of them racing the same file.
    groups: dict[str, dict] = {}
    for path, (content, summary) in collect_image_bumps(base, headers).items():
        g = groups.setdefault(stack_of(path) or path, {"files": {}, "lines": []})
        g["files"][path] = content
        g["lines"].append(summary)
    for path, (content, summary, stack_name) in collect_pin_bumps(base, headers).items():
        g = groups.setdefault(stack_name, {"files": {}, "lines": []})
        g["files"][path] = content
        g["lines"].append(summary)

    for stack, g in sorted(groups.items()):
        what = ", ".join(sorted(g["lines"]))
        open_or_update_mr(base, headers, f"build/{stack}", g["files"],
                          f"{stack}: {what.replace('`', '')}",
                          mr_body(stack, sorted(g["lines"]), sorted(g["files"])), stack)


if __name__ == "__main__":
    main()
