# Roadmap

Deferred decisions and follow-up work, kept here as an example of the
convention the real (private) repo uses: a running log of what's
deliberately not done yet, and why, rather than letting that context
live only in someone's head.

## Still open

- [ ] **Second GitLab Runner.** Still one runner, so jobs queue one at
      a time. Needs its own host (not one this pipeline deploys to -
      see `.gitlab-ci.yml`'s runner-placement warning).
- [ ] **Smart deploy scoping.** `detect-changes` still blocks the whole
      fleet on any `group_vars/`-prefixed change (including a
      `vault.yml.example` comment edit) and silently deploys nothing on
      a `host_vars/`-only change. Vault key-diffing + `env.j2`
      cross-reference would fix this properly; not started.
- [ ] **Verify the apps, not just the pipeline.** A green healthcheck
      isn't the same as a real login/functional check - worth a
      post-deploy smoke-test job per stack eventually.
- [ ] **Nightly drift detection** - considered and declined. The
      deploy is idempotent, so config drift self-corrects on the next
      run rather than needing to be watched for. `remove_orphans: true`
      does mean a container added to a *managed* project by hand gets
      removed on the next deploy - unmanaged projects on the same
      hosts are unaffected.
- [ ] **Secrets backend: ansible-vault -> a self-hosted password
      manager** - deferred, not decided against. Trades a deploy-time
      dependency on a stack that would itself be managed by this same
      pipeline - a real loop, not hypothetical.

## Environment gotchas worth remembering

Still true, still worth knowing before debugging the same thing twice:

- **`group_vars/`/`host_vars/` must live inside `inventory/`.** At the
  project root they're silently ignored in playbook runs (every var
  undefined, every host skipped, exit 0) while `ansible-inventory`
  still shows them - inventory checks won't catch it.
- **A very new Python can trip up an older `ansible-core`** ("Local RPC
  server did not start"). Use a pinned venv, or the `ci-image`
  container directly.
- **ansible-core 2.17 ignores `.ini` inventory files** in a directory
  inventory by default; `ansible.cfg` pins `inventory_ignore_extensions`
  without `.ini`.
- **A task using `connection: local` keeps the real inventory host's
  vars**, including `ansible_python_interpreter`. That path may not
  exist in a CI image with a different Python location.
  `delegate_to: localhost` switches to Ansible's implicit-localhost
  pseudo-host instead, which resolves correctly - `roles/proxmox_snapshot`
  and `playbooks/fleet_report.yml` both rely on this.
- **`ansible-playbook -e "key=${VALUE}"` silently truncates at the
  first space**, shell quoting notwithstanding - it's Ansible's own
  legacy key=value extra-vars parser doing the splitting, not the
  shell. Use `-e @file.json` for any value that might contain
  whitespace.
- **sshd's `AuthorizedKeysFile` varies per host** (some hosts use
  `/etc/ssh/authorized_keys/%u`). `bootstrap.yml` reads `sshd -T` and
  writes wherever sshd actually looks; the wrong path fails silently.
- **Locked accounts (`!` in shadow) can't use pubkey auth** when
  `UsePAM` is off (Alpine default), so the deploy user is created with
  `password: "*"` instead.
- **Alpine hosts ship without Python**, which every Ansible module
  needs; `bootstrap.yml`'s first play installs it via `raw`.

## Declined / deferred

- **A one-off "run arbitrary shell on any host" job** existed briefly
  and was removed: it ran any command as the deploy user -
  root-equivalent on every host with a `become: true` stack - triggered
  by nothing more than a pipeline variable, with no reviewable diff
  first the way a compose change has. If something like it comes back,
  scope it to a fixed menu of safe read-only operations rather than a
  free-form shell string.
