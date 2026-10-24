#!/usr/bin/env python3
"""Print the commit SHA of the last successful auto-deploy job on the
default branch, or nothing if there isn't one or the API can't be read.

detect-changes used to diff CI_COMMIT_BEFORE_SHA..CI_COMMIT_SHA, i.e.
"what did THIS push change". That loses changes whenever a pipeline
never reaches auto-deploy: the image-update bot merges several MRs
within seconds, GitLab auto-cancels the older pipelines as redundant,
and the next pipeline only diffs from the push right before it. The
canceled merges stay on main but never reach a host (ghost, auth and
nomad sat undeployed this way on 2026-10-21/22).

Diffing from the last commit auto-deploy actually succeeded on instead
means a canceled or failed pipeline's changes carry over to the next
one. Only auto-deploy counts: the manual deploy job is usually narrowed
with DEPLOY_LIMIT/STACK_FILTER, so its success says nothing about the
rest of the fleet.

Uses GITLAB_API_TOKEN (see the top of .gitlab-ci.yml), falling back to
CI_JOB_TOKEN. Any failure prints nothing, and the caller falls back to
the old per-push range rather than failing the pipeline.

Usage: last_deployed_sha.py
"""
from __future__ import annotations

import os
import sys

import requests

JOB_NAME = "auto-deploy"
PAGES = 5


def main() -> int:
    api_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_PROJECT_ID")
    branch = os.environ.get("CI_DEFAULT_BRANCH", "main")
    if os.environ.get("GITLAB_API_TOKEN"):
        headers = {"PRIVATE-TOKEN": os.environ["GITLAB_API_TOKEN"]}
    elif os.environ.get("CI_JOB_TOKEN"):
        headers = {"JOB-TOKEN": os.environ["CI_JOB_TOKEN"]}
    else:
        headers = None
    if not (api_url and project_id and headers):
        print("no API credentials - cannot look up the last deploy", file=sys.stderr)
        return 0

    url = f"{api_url}/projects/{project_id}/jobs"
    for page in range(1, PAGES + 1):
        try:
            resp = requests.get(
                url,
                headers=headers,
                params={"scope[]": "success", "per_page": 100, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            jobs = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"jobs API failed: {exc}", file=sys.stderr)
            return 0
        if not jobs:
            break
        # Newest first, so the first match is the last successful deploy.
        for job in jobs:
            if job.get("name") == JOB_NAME and job.get("ref") == branch:
                print(job["commit"]["id"])
                return 0
    print(f"no successful {JOB_NAME} found in the last {PAGES * 100} jobs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
