# deploy-ci-image

CI image for the deploy repo's pipeline, ansible-core, git,
openssh-client, and the collections, all baked in.

Used to install this fresh in every job, but that kept hanging on the
runner (apt-get stuck on `git`, then later on `openssh-client`, no
changes on our end). Pulling one pre-built image instead is just more
reliable.

Published to `ghcr.io/you/deploy-ci-image` by the Actions workflow
here. In the private original this lives as its own repo, embedded in
the deploy repo as a git submodule; in this sanitized copy it's folded
in as a plain directory instead.

## Bumping a pin

Edit the Dockerfile, push. It rebuilds `:latest` automatically.
