# Architecture

How this repo, its pipeline, and the example fleet it manages fit
together. `README.md` is the quick-start; `ROADMAP.md` is what's open
right now. This is the reference for how the whole thing works.

All hostnames, IPs, and domains below are placeholders.

## System overview

```mermaid
flowchart TB
  dev["you\n(admin workstation)"]

  subgraph gh["GitHub (github.com/you)"]
    ciimagerepo["deploy-ci-image repo\nDockerfile + Actions workflow"]
    ghcr["ghcr.io/you/\ndeploy-ci-image"]
    ciimagerepo -->|push to main<br/>GitHub Actions builds| ghcr
  end

  subgraph gitlab_srv["self-hosted GitLab (runs on tools-host)"]
    repo["deploy repo\n.gitlab-ci.yml + ci-image/"]
    runner["runner\nsingle runner, docker executor"]
    repo -->|triggers pipeline| runner
  end

  subgraph pve["Proxmox (node-1 / node-2)"]
    pveapi["Proxmox API\nVM.Snapshot + VM.Audit only\non the managed vmids"]
  end

  subgraph fleet["example fleet"]
    a_t["host-a"]
    b_t["host-b"]
    c_t["host-c"]
  end

  dev -->|git push| repo
  runner -->|pulls image, if-not-present| ghcr
  runner -->|snapshot before every deploy| pveapi
  runner -->|SSH as deploy user\ndocker compose up| a_t & b_t & c_t

  classDef external fill:#495057,color:#fff
  classDef control fill:#2d6a4f,color:#fff
  classDef target fill:#1d3557,color:#fff
  class gh,ciimagerepo,ghcr external
  class repo,runner,pveapi control
  class a_t,b_t,c_t target
```

Three systems, deliberately kept separate:

- **GitHub** builds and hosts the CI image, so the single self-hosted
  runner never installs `ansible-core`/`git`/`openssh-client` fresh on
  every job - see `ci-image/README.md`.
- **GitLab** (self-hosted, running as one of the managed stacks itself)
  owns the pipeline definition and runs the actual jobs.
- **Proxmox** is only ever talked to for snapshots/audit, never for
  provisioning. The API token's ACL is deliberately narrow - it cannot
  roll back a snapshot, only create/list/prune one, and only on the
  vmids it's been granted.

## Repo layout

```mermaid
flowchart LR
  compose["compose/&lt;stack&gt;/\ndocker-compose.yml + env.j2"]
  hostvars["inventory/host_vars/&lt;host&gt;.yml\nstacks: [...], path, become, env_vault"]
  groupvars["inventory/group_vars/\nvault.yml (encrypted), all.yml"]
  role["roles/docker_stack\ncopies files, renders .env,\ndocker compose up, waits healthy"]
  gitrole["roles/git_stack\nclones/pins a repo,\noverlays pinned images"]
  playbook["playbooks/deploy.yml\nsnapshot play + both stack roles"]

  hostvars -->|"stacks: [{name, compose_src, ...}]"| playbook
  hostvars -->|"git_stacks: [{name, repo, version, ...}]"| playbook
  compose -->|copied verbatim| role
  groupvars -->|decrypted vault_* vars| role
  playbook --> role
  playbook --> gitrole
```

A stack only needs two things to exist: a `compose/<name>/` directory,
and an entry in some host's `host_vars/<host>.yml` `stacks:` list
pointing at it. Everything else - directory creation, `.env` rendering,
pull/recreate, the health-check wait - is the same generic
`docker_stack` role for every stack, no per-stack code path.

Two roles exist alongside it for the cases that shape does not fit:

- **`roles/git_stack`** - for a stack whose deployable unit is a whole
  git repo rather than a compose file this repo owns. It checks the repo
  out at a pinned `version:`, renders the same `env.j2`, and applies a
  `compose_overlay` from `compose/<name>/` that pins the images so the
  target never builds. This is the half the companion `build` pipeline
  writes to: it builds the images and opens the merge request that moves
  the pin. See `inventory/host_vars/tools.yml`.
- **`roles/gitlab_config`** - a single-purpose role for settings that
  cannot be passed through compose at all. The GitLab image ignores
  `GITLAB_OMNIBUS_CONFIG` when a `gitlab.rb` already exists, so SMTP,
  OIDC and nginx settings are rendered into that file directly instead.

## How a deploy actually happens

```mermaid
sequenceDiagram
  participant You
  participant GitLab
  participant Runner
  participant Proxmox
  participant Host as target host

  You->>GitLab: git push main (or merge an MR)
  GitLab->>Runner: validate stage: lint, compose-lint, preflight, detect-changes
  Runner->>Runner: detect-changes computes AUTO_STACKS from changed compose/ paths
  alt AUTO_STACKS is empty
    Runner-->>GitLab: auto-deploy exits early, nothing to do
  else AUTO_STACKS has stacks
    Runner->>Proxmox: snapshot every affected host (ci-<timestamp>)
    Proxmox-->>Runner: snapshot confirmed, older ones pruned to last 3
    Runner->>Host: copy compose file + extra_files, render .env from vault
    Runner->>Host: docker compose up (recreate if the copied files changed)
    Runner->>Host: poll until every container is running + healthy
    Host-->>Runner: healthy (or retries exhausted -> fail the job)
    Runner-->>GitLab: deploy summary, one line per stack
  end
```

`gitlab` itself (not part of this example fleet) is the one stack this
can never touch - `FORBIDDEN_STACKS` refuses it even if asked by name,
because recreating the GitLab container would kill the pipeline
running the recreate. Deploy that one from a local `ansible-playbook`
run instead.

## The pipeline's three stages

```mermaid
flowchart TD
  subgraph validate["validate — every push + every MR"]
    lint[lint: playbook syntax-check]
    complint["compose-lint: docker compose config\non every compose/*/docker-compose.yml"]
    preflight["preflight: real inventory load,\nvault decrypt, every invariant checked"]
    detect["detect-changes: diff since last push\n-> AUTO_STACKS / AUTO_HOSTS"]
  end

  subgraph deploy["deploy — main branch only"]
    auto["auto-deploy\nautomatic, scoped to AUTO_STACKS"]
    manual["deploy\nmanual click, DEPLOY_LIMIT + STACK_FILTER"]
  end

  subgraph check["check — schedule or opt-in variable, never on push"]
    imgcheck["check-image-updates\ndaily schedule"]
    freport["fleet-report\nopt-in variable only -\nread-only, no button in every pipeline"]
  end

  validate --> auto
  validate --> manual

  classDef autojob fill:#2d6a4f,color:#fff
  classDef manualjob fill:#495057,color:#fff
  class auto,imgcheck autojob
  class manual,freport manualjob
```

Every job outside `check` (`lint`, `compose-lint`, `preflight`,
`detect-changes`, `deploy`, `auto-deploy`) carries a
`$CI_PIPELINE_SOURCE == "schedule"` -> `when: never` guard first in its
`rules:`, so the daily image-check schedule (or any future one) can
never accidentally also trigger a real deploy.

`resource_group: production` on both `deploy` jobs means only one
deploy can run at a time fleet-wide, even across two separate
pipelines - prevents two concurrent runs racing `docker compose` on
the same host.

## Secrets

```mermaid
flowchart LR
  ci_var["ANSIBLE_VAULT_PASSWORD\nGitLab CI/CD variable, masked"]
  vault["inventory/group_vars/production/vault.yml\nencrypted at rest, in git"]
  envj2["compose/&lt;stack&gt;/env.j2\nJinja template referencing vault_* vars"]
  hostplain["host_vars plain vars\n(non-secret config, e.g. app_url)"]
  rendered[".env on the target host\nmode 0600, deploy-user readable"]

  ci_var -->|decrypts| vault
  vault -->|vault_* values| envj2
  hostplain -->|plain values| envj2
  envj2 -->|rendered by ansible.builtin.template| rendered
  rendered -->|env_file: .env| compose_svc["the running container"]
```

Nothing secret ever touches a compose file directly - every stack that
needs a real secret sets `env_vault: true` in its `host_vars` entry and
reads it via `env.j2`. `preflight` renders every `env.j2` on every push
specifically to catch a missing vault key before it reaches a real
deploy, not after. (`inventory/group_vars/production/vault.yml` itself
is not included in this sanitized copy - see
`vault.yml.example` for its shape.)

## Daily image-update check

```mermaid
flowchart TD
  scan["scan every compose/*/docker-compose.yml\nfor image: lines"]
  classify{pinned by tag\nor by digest?}
  tagcheck["query Docker Hub / ghcr.io\nfor a newer tag on the same\nnumbering track"]
  digestcheck["compare against the tag\nnamed in the comment above it"]
  mr["open/update a bump MR\none per stack, branch image-update/&lt;stack&gt;"]
  issue["open/update an issue\none per stack, label image-update"]
  nothing["nothing to do"]

  scan --> classify
  classify -->|tag| tagcheck
  classify -->|digest| digestcheck
  tagcheck -->|newer stable tag found| mr
  tagcheck -->|up to date| nothing
  digestcheck -->|newer tag exists| issue
  digestcheck -->|up to date| nothing
```

The split is deliberate: a plain tag bump (`ghost:6.45.0` ->
`6.63.0`) is routine enough to be one click to merge. A digest-pinned
image was pinned that way on purpose - usually because the tag doesn't
reproduce what's actually running, or because a migration risk was
called out by hand - so it only ever gets an issue, never a one-click
MR. Neither path ever edits a compose file without a human merging it;
nothing here auto-deploys.

## Where things run, physically (example topology)

| what | lives on |
|---|---|
| GitLab instance | `tools-host`, stack `gitlab` |
| GitLab Runner | same Proxmox cluster, its own host |
| `ci-image` builds | GitHub Actions (not this fleet at all) |
| Proxmox snapshots | taken via API from the runner, stored on each host's own node |

The runner deliberately does not live on any host this pipeline
deploys to - recreating a stack on the runner's own host would kill the
job mid-run.
