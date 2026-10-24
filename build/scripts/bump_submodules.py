#!/usr/bin/env python3
"""Daily check for submodules whose upstream has moved past the pinned
commit, opening a merge request per stale one that does the bump.

A submodule only moves when someone bumps its gitlink. A commit pushed
straight to the source repo leaves the pointer here stale, no image is
built from it, and nothing says so.

This used to open an ISSUE containing the two commands to run by hand.
That recipe named the upstream SHA as it stood when the issue was
written, so an issue that outlived its own commit sent you back to an
older pointer than the one already pinned - which is how a stale pin
reverted a good bump. Committing the bump ourselves removes the step where
a human retypes a SHA.

It opens a merge request and never commits to main, deliberately. A
push to main here builds images, pushes them to Harbor and opens a
merge request against services/deploy; .gitlab-ci.yml's workflow rules
exist to keep a timer from reaching that path unattended. An MR does
not: merge request pipelines run `test` and nothing else, so the bump
publishes nothing until someone merges it.

Compares the gitlink SHA against the remote's default branch with
`git ls-remote` - no clone, no checkout.

Required env: BUILD_API_TOKEN CI_API_V4_URL
Optional env: CVE_PROJECT_PATH (defaults to services/build)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import requests

LABEL = "submodule-bump"
SESSION = requests.Session()


def run(*args: str) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def submodules() -> list[tuple[str, str]]:
    """(path, url) for each registered submodule."""
    out = run("git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    paths = [line.split()[1] for line in out.splitlines() if line.strip()]
    return [(p, run("git", "config", "-f", ".gitmodules", "--get", f"submodule.{p}.url"))
            for p in paths]


def pinned_sha(path: str) -> str:
    """The gitlink this repo's HEAD records for a submodule."""
    return run("git", "rev-parse", f"HEAD:{path}")


def remote_default(url: str) -> tuple[str, str]:
    """(branch, sha) for the remote's default branch, or ("", "") if the
    remote can't be read - a network blip must not read as 'up to date'."""
    out = run("git", "ls-remote", "--symref", url, "HEAD")
    branch, sha = "", ""
    for line in out.splitlines():
        if line.startswith("ref:"):
            branch = line.split()[1].rsplit("/", 1)[-1]
        elif line.split()[1:2] == ["HEAD"]:
            sha = line.split()[0]
    return (branch, sha) if sha and branch else ("", "")


def web_url(url: str) -> str:
    """Browsable base URL for a submodule remote, or "" if it can't be
    derived safely - the MR links a compare view so the reviewer can see
    what they are merging.

    HTTPS remotes only, on purpose. The SSH spelling here is
    ssh.gitlab.example.com:2224, a different host than the web UI's
    gitlab.example.com (see .gitmodules), so deriving a link from it would
    produce a confident 404. No link is better than a wrong one.
    """
    m = re.fullmatch(r"(https://[^/]+/.+?)(?:\.git)?/?", url.strip())
    return m.group(1) if m else ""


# --- GitLab API ---------------------------------------------------------


def api() -> tuple[str, dict]:
    """(base URL for this project, headers). Hard failure without a
    token: a green pipeline that quietly opened nothing looks exactly
    like one that had nothing to open."""
    api_url = os.environ.get("CI_API_V4_URL")
    token = os.environ.get("BUILD_API_TOKEN")
    if not api_url or not token:
        print("CI_API_V4_URL and BUILD_API_TOKEN are required.", file=sys.stderr)
        print("  BUILD_API_TOKEN needs Developer role and `api` scope to push a", file=sys.stderr)
        print("  branch and open a merge request - Reporter is not enough.", file=sys.stderr)
        sys.exit(1)
    path = requests.utils.quote(os.environ.get("CVE_PROJECT_PATH", "services/build"), safe="")
    return f"{api_url}/projects/{path}", {"PRIVATE-TOKEN": token}


def branch_exists(base: str, headers: dict, branch: str) -> bool:
    encoded = requests.utils.quote(branch, safe="")
    resp = SESSION.get(f"{base}/repository/branches/{encoded}", headers=headers, timeout=15)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def create_branch(base: str, headers: dict, branch: str, ref: str = "main") -> None:
    resp = SESSION.post(f"{base}/repository/branches", headers=headers, timeout=15,
                        data={"branch": branch, "ref": ref})
    resp.raise_for_status()


def delete_branch(base: str, headers: dict, branch: str) -> None:
    encoded = requests.utils.quote(branch, safe="")
    resp = SESSION.delete(f"{base}/repository/branches/{encoded}", headers=headers, timeout=15)
    if resp.status_code != 404:
        resp.raise_for_status()


def gitlink_on(base: str, headers: dict, path: str, ref: str) -> str:
    """The SHA a branch's tree records for a submodule, or "". GitLab
    reports a gitlink as a tree entry of type "commit"."""
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    resp = SESSION.get(f"{base}/repository/tree", headers=headers, timeout=15,
                       params={"path": parent, "ref": ref, "per_page": 100})
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    for entry in resp.json():
        if entry.get("type") == "commit" and entry.get("path") == path:
            return entry.get("id", "")
    return ""


def set_gitlink(base: str, headers: dict, path: str, branch: str, sha: str, message: str) -> None:
    """Commit a new gitlink onto a branch. The Submodules API writes the
    160000 tree entry directly; the Commits API only knows about files."""
    encoded = requests.utils.quote(path, safe="")
    resp = SESSION.put(f"{base}/repository/submodules/{encoded}", headers=headers, timeout=20,
                       data={"branch": branch, "commit_sha": sha, "commit_message": message})
    resp.raise_for_status()


def find_mr(base: str, headers: dict, branch: str, state: str = "opened") -> dict | None:
    resp = SESSION.get(f"{base}/merge_requests", headers=headers, timeout=15,
                       params={"state": state, "source_branch": branch, "per_page": 1})
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


# --- what a reviewer reads ----------------------------------------------


def body(path: str, url: str, pinned: str, head: str, branch: str) -> str:
    name = path.rsplit("/", 1)[-1]
    web = web_url(url)
    lines = [
        f"Bumps `{path}` to the tip of its `{branch}`.",
        "",
        f"- was: `{pinned[:12]}`",
        f"- now: `{head[:12]}`",
        f"- source: {url}",
    ]
    if web:
        lines.append(f"- changes: {web}/-/compare/{pinned}...{head}")
    lines += [
        "",
        f"**Merging this builds and pushes `{name}`'s images**, then opens a compose "
        "bump against services/deploy. Nothing is published until then - merge "
        "request pipelines here run the tests only.",
        "",
        f"_Opened automatically by services/build "
        f"({os.environ.get('CI_PIPELINE_URL', 'local run')})._",
    ]
    return "\n".join(lines)


# --- per submodule -------------------------------------------------------


def close_stale_mr(base: str, headers: dict, path: str, branch: str, pinned: str) -> None:
    """Someone bumped this by hand while an MR was open. Its diff is now
    a no-op or, worse, a revert - close it rather than leave a merge
    button that moves the pointer backwards."""
    existing = find_mr(base, headers, branch)
    if not existing:
        return
    SESSION.post(f"{base}/merge_requests/{existing['iid']}/notes", headers=headers, timeout=15,
                 data={"body": f"`{path}` is already pinned at `{pinned[:12]}` on main - "
                               "closing, nothing left to bump."}).raise_for_status()
    SESSION.put(f"{base}/merge_requests/{existing['iid']}", headers=headers, timeout=15,
                data={"state_event": "close"}).raise_for_status()
    print(f"    closed MR !{existing['iid']} - pointer is current again")
    delete_branch(base, headers, branch)


def bump(base: str, headers: dict, path: str, url: str,
         pinned: str, head: str, upstream: str) -> None:
    name = path.rsplit("/", 1)[-1]
    branch = f"bump/{name}"
    existing = find_mr(base, headers, branch)

    # A branch can outlive its merge request: GitLab's remove_source_branch
    # only fires on merge, so a CLOSED MR leaves its branch behind pointing
    # at whatever main looked like then. Committing onto that shows the
    # reviewer every unrelated change main has made since as though this MR
    # introduced it. No open MR owns such a branch, so cut a fresh one.
    if existing is None and branch_exists(base, headers, branch):
        print(f"    {branch} left over from a closed MR - recreating it from main")
        delete_branch(base, headers, branch)
    if not branch_exists(base, headers, branch):
        create_branch(base, headers, branch, "main")

    title = f"{name}: bump submodule to {head[:7]}"
    if gitlink_on(base, headers, path, branch) == head:
        if existing:
            print(f"    MR !{existing['iid']} already at {head[:7]}")
            return
    else:
        set_gitlink(base, headers, path, branch, head, title)

    desc = body(path, url, pinned, head, upstream)
    if existing:
        SESSION.put(f"{base}/merge_requests/{existing['iid']}", headers=headers, timeout=15,
                    data={"title": title, "description": desc}).raise_for_status()
        print(f"    updated MR !{existing['iid']}")
        return
    resp = SESSION.post(f"{base}/merge_requests", headers=headers, timeout=15,
                        data={"source_branch": branch, "target_branch": "main",
                              "title": title, "description": desc, "labels": LABEL,
                              "remove_source_branch": True})
    resp.raise_for_status()
    print(f"    opened MR !{resp.json()['iid']}")


def main() -> None:
    base, headers = api()

    stale = 0
    for path, url in submodules():
        pinned = pinned_sha(path)
        if not pinned:
            print(f"  ! {path}: no gitlink - skipping.", file=sys.stderr)
            continue
        upstream, head = remote_default(url)
        if not head:
            print(f"  ! {path}: could not read {url} - skipping, not assuming current.",
                  file=sys.stderr)
            continue

        if head == pinned:
            print(f"  {path}: current ({pinned[:7]})")
            close_stale_mr(base, headers, path, f"bump/{path.rsplit('/', 1)[-1]}", pinned)
            continue

        stale += 1
        print(f"  {path}: BEHIND - pinned {pinned[:7]}, upstream {head[:7]}")
        bump(base, headers, path, url, pinned, head, upstream)

    print(f"\n{stale} submodule(s) behind.")


if __name__ == "__main__":
    main()
