#!/usr/bin/env python3
"""A GitLab REST API stand-in, just enough of it for open_deploy_mr.py
and bump_submodules.py.

Models the parts whose *interaction* is where the bugs live: branches
that outlive their merge request, `start_branch` being valid only on a
branch that doesn't exist yet, and a file's content differing between
main and a branch cut from an older main.

Gitlinks are tracked alongside files because a submodule bump is not a
file edit - GitLab reports one as a tree entry of type "commit" and
writes it through its own Submodules endpoint.
"""
import json
import re
from urllib.parse import parse_qs, unquote, urlparse


class Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self._body}")


class FakeGitLab:
    """branches: {branch: {path: content}}, gitlinks: {branch: {path: sha}}.
    'main' must exist in both."""

    def __init__(self, main_files, main_gitlinks=None):
        self.branches = {"main": dict(main_files)}
        self.gitlinks = {"main": dict(main_gitlinks or {})}
        self.mrs = []
        self.notes = {}
        self.next_iid = 1
        self.calls = []

    # --- helpers used by tests -------------------------------------------

    def open_mrs(self):
        return [m for m in self.mrs if m["state"] == "opened"]

    def branch_content(self, branch, path):
        return self.branches.get(branch, {}).get(path)

    def commit_to_main(self, path, content):
        """An unrelated change landing on main after a branch was cut."""
        self.branches["main"][path] = content

    def gitlink(self, branch, path):
        return self.gitlinks.get(branch, {}).get(path)

    def _fork(self, src, dst):
        """A new branch is a copy of its ref, gitlinks included."""
        self.branches[dst] = dict(self.branches[src])
        self.gitlinks[dst] = dict(self.gitlinks.get(src, {}))

    # --- the transport ----------------------------------------------------

    def _route(self, method, url):
        parsed = urlparse(url)
        # everything after .../projects/<encoded path>/
        tail = parsed.path.split("/projects/", 1)[1].split("/", 1)[1]
        return method, tail, parse_qs(parsed.query)

    def get(self, url, headers=None, params=None, timeout=None):
        method, tail, _ = self._route("GET", url)
        params = params or {}
        self.calls.append((method, tail))

        m = re.fullmatch(r"repository/files/(.+)/raw", tail)
        if m:
            path, ref = unquote(m.group(1)), params.get("ref")
            if ref not in self.branches:
                return Response(404, {"message": "404 Branch Not Found"})
            content = self.branches[ref].get(path)
            return Response(200, content) if content is not None else Response(404, {})

        m = re.fullmatch(r"repository/branches/(.+)", tail)
        if m:
            branch = unquote(m.group(1))
            return Response(200, {"name": branch}) if branch in self.branches else Response(404, {})

        if tail == "repository/tree":
            # Directory listing, derived from whatever files the test put
            # on the branch rather than a second thing to keep in sync.
            prefix = (params.get("path") or "").rstrip("/") + "/"
            ref = params.get("ref", "main")
            if ref not in self.branches:
                return Response(404, {})
            names = sorted({p for p in self.branches[ref] if p.startswith(prefix)})
            out = [{"path": p, "type": "blob", "name": p.rsplit("/", 1)[-1]}
                   for p in names]
            out += [{"path": p, "type": "commit", "id": sha, "name": p.rsplit("/", 1)[-1]}
                    for p, sha in sorted(self.gitlinks.get(ref, {}).items())
                    if p.startswith(prefix)]
            return Response(200, out)

        if tail == "merge_requests":
            src, state = params.get("source_branch"), params.get("state")
            out = [m for m in self.mrs
                   if (state is None or m["state"] == state)
                   and (src is None or m["source_branch"] == src)]
            return Response(200, out)

        raise AssertionError(f"unrouted GET {tail}")

    def post(self, url, headers=None, json=None, data=None, timeout=None):
        method, tail, _ = self._route("POST", url)
        self.calls.append((method, tail))

        if tail == "repository/commits":
            payload = json
            branch, start = payload["branch"], payload.get("start_branch")
            if start is not None and branch in self.branches:
                # The real API's behaviour, and the bug commit 09b2a49 fixed.
                return Response(400, {"message": "Branch already exists"})
            if start is not None:
                if start not in self.branches:
                    return Response(400, {"message": "start_branch missing"})
                self._fork(start, branch)
            elif branch not in self.branches:
                return Response(400, {"message": "Branch Not Found"})
            for action in payload["actions"]:
                assert action["action"] == "update"
                self.branches[branch][action["file_path"]] = action["content"]
            return Response(201, {"id": "deadbeef"})

        if tail == "repository/branches":
            branch, ref = data["branch"], data["ref"]
            if branch in self.branches:
                return Response(400, {"message": "Branch already exists"})
            if ref not in self.branches:
                return Response(400, {"message": "Invalid reference name"})
            self._fork(ref, branch)
            return Response(201, {"name": branch})

        m = re.fullmatch(r"merge_requests/(\d+)/notes", tail)
        if m:
            self.notes.setdefault(int(m.group(1)), []).append(data["body"])
            return Response(201, {"id": 1})

        if tail == "merge_requests":
            mr = {
                "iid": self.next_iid,
                "source_branch": data["source_branch"],
                "target_branch": data["target_branch"],
                "title": data["title"],
                "description": data["description"],
                "state": "opened",
            }
            self.next_iid += 1
            self.mrs.append(mr)
            return Response(201, mr)

        raise AssertionError(f"unrouted POST {tail}")

    def put(self, url, headers=None, json=None, data=None, timeout=None):
        method, tail, _ = self._route("PUT", url)
        self.calls.append((method, tail))

        m = re.fullmatch(r"repository/submodules/(.+)", tail)
        if m:
            path, branch = unquote(m.group(1)), data["branch"]
            if branch not in self.branches:
                return Response(400, {"message": "Branch Not Found"})
            self.gitlinks.setdefault(branch, {})[path] = data["commit_sha"]
            return Response(200, {"id": "deadbeef"})

        m = re.fullmatch(r"merge_requests/(\d+)", tail)
        if m:
            iid = int(m.group(1))
            for mr in self.mrs:
                if mr["iid"] == iid:
                    mr.update({k: v for k, v in (data or {}).items() if k != "state_event"})
                    if (data or {}).get("state_event") == "close":
                        mr["state"] = "closed"
                    return Response(200, mr)
            return Response(404, {})
        raise AssertionError(f"unrouted PUT {tail}")

    def delete(self, url, headers=None, timeout=None):
        method, tail, _ = self._route("DELETE", url)
        self.calls.append((method, tail))
        m = re.fullmatch(r"repository/branches/(.+)", tail)
        if m:
            branch = unquote(m.group(1))
            if branch == "main":
                return Response(403, {"message": "protected"})
            if branch in self.branches:
                del self.branches[branch]
                self.gitlinks.pop(branch, None)
                return Response(204, {})
            return Response(404, {})
        raise AssertionError(f"unrouted DELETE {tail}")
