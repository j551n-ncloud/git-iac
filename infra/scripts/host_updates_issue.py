#!/usr/bin/env python3
"""Keep one GitLab issue in sync with host_updates.json.

playbooks/updates_report.yml writes host_updates.json (pending apt
updates per [base] host). This files one issue labelled `host-updates`
while anything is pending, updates it on every run, and closes it once
every host is up to date. Without GitLab credentials in the environment
it only prints the summary.

    python3 scripts/host_updates_issue.py [host_updates.json]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

LABEL = "host-updates"
STALE_LISTS_DAYS = 2


def gitlab_api():
    """(api_url, project_id, headers), or None without credentials.
    GITLAB_API_TOKEN (project access token) first: some instances refuse
    CI_JOB_TOKEN on the Issues API."""
    api_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_PROJECT_ID")
    if not (api_url and project_id):
        return None
    if os.environ.get("GITLAB_API_TOKEN"):
        return api_url, project_id, {"PRIVATE-TOKEN": os.environ["GITLAB_API_TOKEN"]}
    if os.environ.get("CI_JOB_TOKEN"):
        return api_url, project_id, {"JOB-TOKEN": os.environ["CI_JOB_TOKEN"]}
    return None


def needs_attention(h: dict) -> bool:
    return bool(h["upgradable"]) or h["reboot_required"] or h["lists_age_days"] > STALE_LISTS_DAYS


def title(hosts: list[dict], unreachable: list[str]) -> str:
    pending = [h for h in hosts if h["upgradable"]]
    sec = sum(len(h["security"]) for h in hosts)
    reboot = [h for h in hosts if h["reboot_required"]]
    parts = [f"{len(pending)} host(s) with pending updates"]
    if sec:
        parts.append(f"{sec} security")
    if reboot:
        parts.append(f"{len(reboot)} need a reboot")
    if unreachable:
        parts.append(f"{len(unreachable)} unreachable")
    return "Host updates: " + ", ".join(parts)


def body(hosts: list[dict], unreachable: list[str]) -> str:
    lines = [
        "Pending apt updates on the `[base]` hosts. Security updates install "
        "daily through unattended-upgrades (Docker excluded), so what is left "
        "here waits for a patch run:",
        "",
        "```",
        "ansible-playbook playbooks/patch.yml -e @.proxmox.json",
        "```",
        "",
        "| Host | Pending | Security | Reboot | Held | Lists age |",
        "|---|---|---|---|---|---|",
    ]
    for h in hosts:
        age = h["lists_age_days"]
        age_s = "unknown" if age < 0 else f"{age} d" + (" (stale)" if age > STALE_LISTS_DAYS else "")
        lines.append(
            f"| {h['host']} | {len(h['upgradable'])} | {len(h['security'])} | "
            f"{'yes' if h['reboot_required'] else 'no'} | {', '.join(h['held']) or '-'} | {age_s} |"
        )
    for name in unreachable:
        lines.append(f"| {name} | unreachable or failed | | | | |")
    for h in hosts:
        if h["upgradable"]:
            lines += ["", f"<details><summary>{h['host']}: {len(h['upgradable'])} package(s)</summary>", "", "```"]
            lines += h["upgradable"]
            lines += ["```", "</details>"]
    lines += ["", f"_Updated by the `check-host-updates` schedule ({os.environ.get('CI_PIPELINE_URL', 'local run')})._"]
    return "\n".join(lines)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "host_updates.json")
    data = json.loads(path.read_text())
    hosts = sorted(data["hosts"], key=lambda h: h["host"])
    unreachable = sorted(data.get("unreachable", []))
    attention = bool(unreachable) or any(needs_attention(h) for h in hosts)
    print(title(hosts, unreachable) if attention else "Host updates: everything up to date")

    creds = gitlab_api()
    if creds is None:
        print("no GitLab API credentials in the environment - printing results only.")
        return
    api_url, project_id, headers = creds
    base = f"{api_url}/projects/{project_id}/issues"
    resp = requests.get(base, headers=headers, params={"state": "opened", "labels": LABEL}, timeout=15)
    resp.raise_for_status()
    existing = resp.json()[0] if resp.json() else None

    if attention:
        data = {"title": title(hosts, unreachable), "description": body(hosts, unreachable)}
        if existing:
            requests.put(f"{base}/{existing['iid']}", headers=headers, data=data, timeout=15).raise_for_status()
            print(f"updated issue #{existing['iid']}")
        else:
            r = requests.post(base, headers=headers, data={**data, "labels": LABEL}, timeout=15)
            r.raise_for_status()
            print(f"filed issue #{r.json()['iid']}")
    elif existing:
        requests.put(f"{base}/{existing['iid']}", headers=headers, data={"state_event": "close"},
                     timeout=15).raise_for_status()
        print(f"closed issue #{existing['iid']} (all hosts up to date)")


if __name__ == "__main__":
    main()
