#!/usr/bin/env python3
"""Daily CVE check for the images this repo builds, opening a GitLab
issue per affected image. Deck card 24.

No scanner runs here: Harbor already Trivy-scans every artifact on
push, so this reads `scan_overview` over the API instead of re-pulling
hundreds of MB nightly to recompute the same numbers.

Scope is this repo's submodules, not the whole Harbor project - that
also holds images nothing here builds.

CVE_ISSUE_ON picks the gate: `fixable` (default, a patched version
exists) or `critical`. The card said critical; fixable is the default
because it is the actionable one - dashboard-api sits at 0 Critical
and 0 fixable but 45 High, which a Critical gate calls green.

Required env: HARBOR_URL HARBOR_PROJECT HARBOR_ROBOT_USER
              HARBOR_ROBOT_TOKEN BUILD_API_TOKEN CI_API_V4_URL
Optional env: CVE_ISSUE_ON (fixable|critical), CVE_PROJECT_PATH
"""
from __future__ import annotations

import os
import sys

import requests

LABEL = "cve-scan"
SESSION = requests.Session()
SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Unknown"]


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default if default is not None else "")
    if not value:
        print(f"{name} is empty - see .gitlab-ci.yml's header comment.", file=sys.stderr)
        sys.exit(1)
    return value


def load_image_names(path: str = "image_names.txt") -> list[str]:
    """One image name per line, written by the CI job from
    discover-images.sh - the same discovery build-and-push uses, so an
    image is scanned for exactly as long as it is built."""
    if not os.path.exists(path):
        return []
    seen = []
    for line in open(path):
        name = line.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


# buildx pushes its layer cache as a tag in the same repository, at the
# same moment as the image. It is not an image, Harbor never scans it,
# and it is frequently the NEWEST artifact - so taking the newest
# blindly checked the cache and reported "no completed scan" for the two
# images with the most CVE history.
CACHE_TAGS = {"buildcache"}


def latest_artifact(harbor: str, project: str, auth, name: str) -> dict | None:
    """Newest pushed real image for one repository, with its scan
    results - skipping layer-cache tags."""
    repo = requests.utils.quote(name, safe="")
    url = f"https://{harbor}/api/v2.0/projects/{project}/repositories/{repo}/artifacts"
    resp = SESSION.get(url, auth=auth, timeout=30, params={
        "page_size": 20, "sort": "-push_time", "with_scan_overview": "true"})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for artifact in resp.json():
        tags = {t["name"] for t in (artifact.get("tags") or [])}
        if tags and not tags <= CACHE_TAGS:
            return artifact
    return None


def scan_summary(artifact: dict) -> dict | None:
    """Harbor nests this under a mime type that has changed between
    versions, so take whichever key is present rather than hardcoding
    one and silently reporting zero findings when it changes."""
    overview = artifact.get("scan_overview") or {}
    for value in overview.values():
        summary = value.get("summary") or {}
        return {
            "scan_status": value.get("scan_status"),
            "total": summary.get("total", 0),
            "fixable": summary.get("fixable", 0),
            "severity": summary.get("summary", {}),
        }
    return None


def issue_body(name: str, tag: str, scan: dict, gate: str) -> str:
    sev = scan["severity"]
    rows = [f"- {k}: {sev[k]}" for k in SEVERITY_ORDER if sev.get(k)]
    return "\n".join([
        f"Harbor's Trivy scan of the newest `{name}` image:",
        "",
        f"- tag: `{tag}`",
        f"- total findings: {scan['total']}",
        f"- **fixable: {scan['fixable']}**",
        "",
        "Severity breakdown:",
        *(rows or ["- (none)"]),
        "",
        f"Opened because `CVE_ISSUE_ON={gate}` matched. A fixable finding means a "
        "patched version exists upstream, so the fix is a dependency or base-image "
        "bump in the submodule, then a rebuild.",
        "",
        "This issue is updated in place by each daily run and closed automatically "
        "once the newest image is clean, so it always reflects the current image "
        "rather than the day it was opened.",
        "",
        f"_Opened automatically by services/build "
        f"({os.environ.get('CI_PIPELINE_URL', 'local run')})._",
    ])


def find_issue(base: str, headers: dict, title: str) -> dict | None:
    resp = SESSION.get(f"{base}/issues", headers=headers, timeout=15,
                       params={"labels": LABEL, "search": title, "in": "title",
                               "state": "all", "per_page": 20})
    resp.raise_for_status()
    for issue in resp.json():
        if issue["title"] == title:
            return issue
    return None


def upsert_issue(base: str, headers: dict, title: str, body: str) -> None:
    existing = find_issue(base, headers, title)
    if existing is None:
        resp = SESSION.post(f"{base}/issues", headers=headers, timeout=15,
                            data={"title": title, "description": body, "labels": LABEL})
        resp.raise_for_status()
        print(f"  opened issue #{resp.json()['iid']}: {title}")
        return
    resp = SESSION.put(f"{base}/issues/{existing['iid']}", headers=headers, timeout=15,
                       data={"description": body, "state_event": "reopen"})
    resp.raise_for_status()
    state = "reopened" if existing["state"] == "closed" else "updated"
    print(f"  {state} issue #{existing['iid']}: {title}")


def close_issue_if_open(base: str, headers: dict, title: str) -> None:
    """Only ever closes an issue carrying this automation's own label -
    a human's issue about the same image is not this job's to tidy."""
    existing = find_issue(base, headers, title)
    if existing is None or existing["state"] == "closed":
        return
    SESSION.post(f"{base}/issues/{existing['iid']}/notes", headers=headers, timeout=15,
                 data={"body": "Newest image is clean - closing automatically."}).raise_for_status()
    resp = SESSION.put(f"{base}/issues/{existing['iid']}", headers=headers, timeout=15,
                       data={"state_event": "close"})
    resp.raise_for_status()
    print(f"  closed issue #{existing['iid']}: {title}")


def main() -> None:
    names = load_image_names()
    if not names:
        print("no images discovered - nothing to scan.")
        return

    harbor, project = env("HARBOR_URL"), env("HARBOR_PROJECT")
    auth = (env("HARBOR_ROBOT_USER"), env("HARBOR_ROBOT_TOKEN"))
    gate = os.environ.get("CVE_ISSUE_ON", "fixable")
    if gate not in ("fixable", "critical"):
        print(f"CVE_ISSUE_ON must be 'fixable' or 'critical', got {gate!r}", file=sys.stderr)
        sys.exit(1)

    api = env("CI_API_V4_URL")
    token = env("BUILD_API_TOKEN")
    path = requests.utils.quote(os.environ.get("CVE_PROJECT_PATH", "services/build"), safe="")
    base, headers = f"{api}/projects/{path}", {"PRIVATE-TOKEN": token}

    findings = 0
    for name in names:
        artifact = latest_artifact(harbor, project, auth, name)
        if artifact is None:
            print(f"  {name}: not in Harbor yet - skipping.")
            continue
        tags = [t["name"] for t in (artifact.get("tags") or [])]
        tag = tags[0] if tags else "<untagged>"
        scan = scan_summary(artifact)
        if scan is None or scan["scan_status"] != "Success":
            # Not a clean bill of health: an unscanned image is unknown,
            # not safe, and saying nothing would read as the latter.
            print(f"  ! {name}:{tag}: no completed scan in Harbor "
                  f"(status={scan['scan_status'] if scan else 'none'}).", file=sys.stderr)
            continue

        critical = scan["severity"].get("Critical", 0)
        hit = scan["fixable"] > 0 if gate == "fixable" else critical > 0
        title = f"CVEs in {name}"
        summary = (f"{scan['total']} total, {scan['fixable']} fixable, "
                   f"{critical} critical")
        if hit:
            findings += 1
            print(f"  {name}:{tag}: {summary}")
            upsert_issue(base, headers, title, issue_body(name, tag, scan, gate))
        else:
            print(f"  {name}:{tag}: clean by {gate} ({summary})")
            close_issue_if_open(base, headers, title)

    print(f"\n{findings} of {len(names)} image(s) have findings.")


if __name__ == "__main__":
    main()
