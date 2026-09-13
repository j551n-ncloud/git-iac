# dashboard

Sample project standing in for a real one. Shows the monorepo shape: two
nested `Dockerfile`s, which discovery turns into two images named
`<project>-<subdir>`, so `dashboard-api` and `dashboard-worker`.

Both move together. They share one commit SHA, so one pipeline run
builds both and they land in a single merge request against the deploy
repo.

In the real setup this directory is a git submodule pointing at its own
source repo. Here it is checked in directly so the fork is clonable and
the pipeline is runnable without access to any private repo.
