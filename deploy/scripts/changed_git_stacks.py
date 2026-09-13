#!/usr/bin/env python3
"""Print `host<TAB>stack` for every git_stacks entry whose pinned
`version:` actually moved in this push.

Compose stacks are detected by path - a change under compose/<name>/ is
a change to that stack, which .gitlab-ci.yml's detect-changes job reads
straight off `git diff --name-only`. A git stack (roles/git_stack) has
no compose file: it is pinned by a commit SHA in
inventory/host_vars/<host>.yml, so that path-based rule can never see
one. Merging the pin bump therefore deployed nothing, which is the gap
this closes.

Comparing the parsed value before and after, rather than grepping the
diff for `version:`, is what keeps it honest in both directions:

  * editing a comment, adding a stack, or reformatting the same file
    must NOT redeploy anything - a diff-grep would fire on any hunk
    that happens to include a version line as context;
  * a pin that moved must be caught even if the diff is oddly framed.

Unknown YAML tags (!vault) are tolerated: host_vars can carry encrypted
values, and this only ever reads these files.

Usage: changed_git_stacks.py <before-ref>   (reads changed.txt in cwd)
"""
from __future__ import annotations

import os
import subprocess
import sys

import yaml


class _TolerantLoader(yaml.SafeLoader):
    pass


_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def pins(text: str) -> dict[str, str]:
    """stack name -> pinned version, for one host_vars file's content."""
    if not text.strip():
        return {}
    try:
        data = yaml.load(text, Loader=_TolerantLoader) or {}
    except yaml.YAMLError:
        # A file we cannot parse is not a reason to deploy something.
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for stack in data.get("git_stacks") or []:
        if isinstance(stack, dict) and stack.get("name"):
            out[str(stack["name"])] = str(stack.get("version", ""))
    return out


def file_at(ref: str, path: str) -> str:
    """The file's content at a git ref, or "" if it didn't exist there -
    which is also how a newly added git stack reads: every entry looks
    changed, which is correct, it has never been deployed."""
    proc = subprocess.run(["git", "show", f"{ref}:{path}"],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        # No usable base ref (first push to a branch, say). Detecting
        # nothing is the safe answer: the manual deploy job still works.
        return
    base = sys.argv[1].strip()

    for line in open("changed.txt"):
        path = line.strip()
        if not path.startswith("inventory/host_vars/"):
            continue
        if not path.endswith((".yml", ".yaml")):
            continue
        host = os.path.basename(path).rsplit(".", 1)[0]
        before = pins(file_at(base, path))
        after = pins(open(path).read()) if os.path.exists(path) else {}
        for name, version in sorted(after.items()):
            if before.get(name) != version:
                print(f"{host}\t{name}")


if __name__ == "__main__":
    main()
