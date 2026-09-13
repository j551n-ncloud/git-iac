#!/usr/bin/env python3
"""Daily check for submodules whose upstream has moved past the pinned
commit, opening a GitLab issue per stale one.

A submodule only moves when someone runs sync-submodule.sh. A commit
pushed straight to the source repo leaves the pointer here stale, no
image is built from it, and nothing says so.

Compares the gitlink SHA against the remote's default branch with
`git ls-remote` - no clone, no checkout.

Required env: BUILD_API_TOKEN CI_API_V4_URL
Optional env: CVE_PROJECT_PATH (defaults to services/build)
"""
from __future__ import annotations

import os
import subprocess
import sys

import requests

LABEL = "submodule-stale"
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


def remote_head(url: str) -> str:
    """The default branch's SHA, or "" if the remote can't be read - a
    network blip must not read as 'up to date'."""
    out = run("git", "ls-remote", url, "HEAD")
    return out.split()[0] if out else ""


def find_issue(base: str, headers: dict, title: str) -> dict | None:
    resp = SESSION.get(f"{base}/issues", headers=headers, timeout=15,
                       params={"labels": LABEL, "search": title, "in": "title",
                               "state": "all", "per_page": 20})
    resp.raise_for_status()
    return next((i for i in resp.json() if i["title"] == title), None)


def body(path: str, url: str, pinned: str, head: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return "\n".join([
        f"`{path}` is behind its source.",
        "",
        f"- pinned here: `{pinned[:12]}`",
        f"- upstream HEAD: `{head[:12]}`",
        f"- source: {url}",
        "",
        "Nothing has been built from the newer commits. To pick them up:",
        "",
        "```bash",
        f"git -C {path} fetch origin && git -C {path} checkout {head[:12]}",
        f"scripts/sync-submodule.sh {name} \"{name}: bump submodule\"",
        "```",
        "",
        f"_Opened automatically by services/build "
        f"({os.environ.get('CI_PIPELINE_URL', 'local run')})._",
    ])


def main() -> None:
    api = os.environ.get("CI_API_V4_URL")
    token = os.environ.get("BUILD_API_TOKEN")
    if not api or not token:
        print("CI_API_V4_URL and BUILD_API_TOKEN are required.", file=sys.stderr)
        sys.exit(1)
    path_enc = requests.utils.quote(
        os.environ.get("CVE_PROJECT_PATH", "services/build"), safe="")
    base, headers = f"{api}/projects/{path_enc}", {"PRIVATE-TOKEN": token}

    stale = 0
    for path, url in submodules():
        pinned = run("git", "rev-parse", f"HEAD:{path}")
        if not pinned:
            print(f"  ! {path}: no gitlink - skipping.", file=sys.stderr)
            continue
        head = remote_head(url)
        if not head:
            print(f"  ! {path}: could not read {url} - skipping, not assuming current.",
                  file=sys.stderr)
            continue

        title = f"{path} is behind its source"
        existing = find_issue(base, headers, title)
        if head == pinned:
            print(f"  {path}: current ({pinned[:7]})")
            if existing and existing["state"] == "opened":
                SESSION.post(f"{base}/issues/{existing['iid']}/notes", headers=headers,
                             timeout=15, data={"body": "Pointer is current again - closing."}
                             ).raise_for_status()
                SESSION.put(f"{base}/issues/{existing['iid']}", headers=headers, timeout=15,
                            data={"state_event": "close"}).raise_for_status()
                print(f"    closed issue #{existing['iid']}")
            continue

        stale += 1
        print(f"  {path}: BEHIND - pinned {pinned[:7]}, upstream {head[:7]}")
        if existing is None:
            r = SESSION.post(f"{base}/issues", headers=headers, timeout=15,
                             data={"title": title, "description": body(path, url, pinned, head),
                                   "labels": LABEL})
            r.raise_for_status()
            print(f"    opened issue #{r.json()['iid']}")
        else:
            SESSION.put(f"{base}/issues/{existing['iid']}", headers=headers, timeout=15,
                        data={"description": body(path, url, pinned, head),
                              "state_event": "reopen"}).raise_for_status()
            print(f"    updated issue #{existing['iid']}")

    print(f"\n{stale} submodule(s) behind.")


if __name__ == "__main__":
    main()
