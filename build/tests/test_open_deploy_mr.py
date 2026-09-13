#!/usr/bin/env python3
"""open_deploy_mr.py writes to services/deploy, the repo that actually
runs this fleet. Anything it gets wrong lands as a merge request someone
is expected to trust, so the interesting cases are the ones that produce
a *plausible-looking* MR with the wrong diff in it.
"""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_gitlab import FakeGitLab  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = "compose/docs-site/docker-compose.yml"

MAIN_COMPOSE = """\
services:
  docs-site:
    image: registry.example.com/library/docs-site:aaaaaaa
    restart: unless-stopped
    ports:
      - "3000:3000"
"""


def load_module():
    """Fresh import per test - the module keeps a module-level SESSION."""
    spec = importlib.util.spec_from_file_location(
        "open_deploy_mr", os.path.join(REPO, "scripts", "open_deploy_mr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MRTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))

        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))
        os.environ["CI_API_V4_URL"] = "https://gitlab.example.com/api/v4"
        os.environ["DEPLOY_API_TOKEN"] = "token"
        os.environ["DEPLOY_PROJECT_PATH"] = "services/deploy"

        self.mod = load_module()
        self.gl = FakeGitLab({COMPOSE: MAIN_COMPOSE})
        self.mod.SESSION = self.gl

    def write_built(self, **images):
        with open("built_images.env", "w") as f:
            for name, ref in images.items():
                f.write(f"{name}_IMAGE={ref}\n")

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.mod.main()
        return out.getvalue(), err.getvalue()


class StaleBranchTest(MRTestCase):
    def test_bump_onto_branch_left_by_a_closed_mr_keeps_main_changes(self):
        """The failure this guards against: `build/docs-site` survives a
        CLOSED merge request (GitLab only deletes a source branch on
        merge), main moves on, and the next bump commits onto the stale
        branch instead of a fresh one. The resulting MR then silently
        reverts whatever else main changed in that file - and
        check_image_updates.py in services/deploy edits this very file,
        so the collision is real, not hypothetical.
        """
        # A bump branch from an earlier run, whose MR was closed not merged.
        self.gl.branches["build/docs-site"] = {
            COMPOSE: MAIN_COMPOSE.replace("aaaaaaa", "bbbbbbb")}
        self.gl.mrs.append({
            "iid": 1, "source_branch": "build/docs-site", "target_branch": "main",
            "title": "old", "description": "", "state": "closed"})

        # Meanwhile main gained an unrelated change to the same file.
        self.gl.commit_to_main(COMPOSE, MAIN_COMPOSE.replace(
            '      - "3000:3000"\n',
            '      - "3000:3000"\n    environment:\n      - NODE_ENV=production\n'))

        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()

        result = self.gl.branch_content("build/docs-site", COMPOSE)
        self.assertIn("docs-site:ccccccc", result, "the bump itself must be applied")
        self.assertIn("NODE_ENV=production", result,
                      "main's unrelated change must survive the bump")
        self.assertNotIn("aaaaaaa", result)

    def test_stale_branch_reuse_does_not_400(self):
        """The regression guard for commit 09b2a49: re-supplying
        start_branch against an existing branch is a 400. Whatever the
        fix for the staleness is, it must not reintroduce that."""
        self.gl.branches["build/docs-site"] = {
            COMPOSE: MAIN_COMPOSE.replace("aaaaaaa", "bbbbbbb")}
        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()  # must not raise
        self.assertEqual(len(self.gl.open_mrs()), 1)

    def test_stale_branch_is_recreated_so_the_diff_is_one_line(self):
        """No open MR owns a branch left by a closed one, so it gets cut
        fresh from main. Without this the bump is correct but diffs
        against an old merge base, showing main's unrelated changes as
        though this MR made them - and reading that diff is the only
        thing a human does in this whole pipeline."""
        self.gl.branches["build/docs-site"] = {
            COMPOSE: MAIN_COMPOSE.replace("aaaaaaa", "bbbbbbb")}
        self.gl.mrs.append({
            "iid": 1, "source_branch": "build/docs-site", "target_branch": "main",
            "title": "old", "description": "", "state": "closed"})

        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()

        self.assertIn(("DELETE", "repository/branches/build%2Fdocs-site"), self.gl.calls,
                      "the orphaned branch should be deleted, not committed onto")
        self.assertEqual(len(self.gl.open_mrs()), 1)

    def test_branch_of_an_open_mr_is_never_deleted(self):
        """Deleting a branch someone is reviewing would close their MR
        and throw away the discussion on it."""
        self.gl.branches["build/docs-site"] = {
            COMPOSE: MAIN_COMPOSE.replace("aaaaaaa", "bbbbbbb")}
        self.gl.mrs.append({
            "iid": 3, "source_branch": "build/docs-site", "target_branch": "main",
            "title": "open", "description": "", "state": "opened"})

        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()

        self.assertFalse([c for c in self.gl.calls if c[0] == "DELETE"])
        self.assertEqual([m["iid"] for m in self.gl.open_mrs()], [3])

    def test_open_mr_is_updated_in_place_not_recreated(self):
        """An MR someone is already reviewing must keep its iid, its
        discussion and its branch - only the bump on top changes."""
        self.gl.branches["build/docs-site"] = {
            COMPOSE: MAIN_COMPOSE.replace("aaaaaaa", "bbbbbbb")}
        self.gl.mrs.append({
            "iid": 7, "source_branch": "build/docs-site", "target_branch": "main",
            "title": "docs-site: bump to bbbbbbb", "description": "", "state": "opened"})

        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()

        self.assertEqual([m["iid"] for m in self.gl.open_mrs()], [7])
        self.assertIn("ccccccc", self.gl.branch_content("build/docs-site", COMPOSE))

    def test_rerun_with_no_new_image_is_a_noop(self):
        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()
        self.assertEqual(len(self.gl.open_mrs()), 1)
        before = len(self.gl.mrs)
        self.run_main()
        self.assertEqual(len(self.gl.mrs), before, "no duplicate MR on a re-run")


class CredentialsTest(MRTestCase):
    def test_missing_token_fails_loudly(self):
        """A green pipeline that opened no MR is indistinguishable from
        one with nothing to do. services/deploy refuses that trade in
        .ansible_vault and .proxmox_creds; so should this."""
        del os.environ["DEPLOY_API_TOKEN"]
        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        with self.assertRaises(SystemExit) as cm:
            self.run_main()
        self.assertEqual(cm.exception.code, 1)

    def test_nothing_built_is_not_an_error(self):
        """No token needed when there was nothing to do - a
        README-only push must stay green."""
        del os.environ["DEPLOY_API_TOKEN"]
        out, _ = self.run_main()
        self.assertIn("nothing to do", out)


class BumpLineTest(MRTestCase):
    def test_only_the_matching_image_line_changes(self):
        content = (MAIN_COMPOSE +
                   "  sidecar:\n    image: docker.io/library/redis:7\n")
        self.gl.branches["main"][COMPOSE] = content
        self.write_built(**{"docs-site": "registry.example.com/library/docs-site:ccccccc"})
        self.run_main()
        result = self.gl.branch_content("build/docs-site", COMPOSE)
        self.assertIn("redis:7", result)
        self.assertIn("docs-site:ccccccc", result)

    def test_unmatched_image_is_skipped_not_guessed(self):
        self.write_built(**{"docs-site": "registry.example.com/other/docs-site:ccccccc"})
        _, err = self.run_main()
        self.assertEqual(self.gl.open_mrs(), [])
        self.assertIn("skipping", err)

    def test_missing_stack_is_skipped_not_created(self):
        self.write_built(**{"dashboard-worker": "registry.example.com/library/dashboard-worker:ddddddd"})
        _, err = self.run_main()
        self.assertEqual(self.gl.open_mrs(), [])
        self.assertIn("dashboard-worker", err)


# --- git stacks: pinned by submodule commit, not image tag -----------------

HOST_VARS = "inventory/host_vars/tools.yml"
OLD_SHA = "5462bc2f434e6b86fcb0f0e029679b56987120c0"
NEW_SHA = "4c8104d3d25561f67c011b6ad850866612091acb"

MAIN_HOST_VARS = f"""\
---
# Busiest host in the fleet. Every line of this comment matters to
# whoever reads the next diff against this file.
stacks:
  - name: gitlab
    compose_src: compose/gitlab
    health_retries: 180

# Stacks whose deployable unit is the whole git repo.
git_stacks:
  - name: dashboard
    repo: git@github.com:example-org/dashboard.git
    # Kept at CasaOS's own path, see the role's header for why.
    path: /data/dashboard
    version: {OLD_SHA}
    become: true
    env_vault: true
    env_src: compose/dashboard
    health_retries: 60

proxmox_node: node-2
"""


class GitStackTestCase(MRTestCase):
    def setUp(self):
        super().setUp()
        self.gl.branches["main"][HOST_VARS] = MAIN_HOST_VARS

    def write_submodules(self, *rows):
        with open("built_submodules.tsv", "w") as f:
            for row in rows:
                f.write("\t".join(row) + "\n")


class NormalizeRepoTest(MRTestCase):
    def test_ssh_and_https_forms_of_one_repo_match(self):
        n = self.mod.normalize_repo
        self.assertEqual(n("git@github.com:example-org/dashboard.git"),
                         n("https://github.com/example-org/dashboard"))

    def test_different_repos_do_not_match(self):
        n = self.mod.normalize_repo
        self.assertNotEqual(n("git@github.com:example-org/dashboard.git"),
                            n("git@github.com:example-org/docs-site.git"))

    def test_host_is_part_of_the_identity(self):
        """Same path on two forges is two different repos - matching on
        the path alone would bump a pin to a commit from elsewhere."""
        n = self.mod.normalize_repo
        self.assertNotEqual(n("https://github.com/example-group/x.git"),
                            n("https://gitlab.example.com/example-group/x.git"))


class PinBumpTest(GitStackTestCase):
    def test_pin_is_bumped_and_an_mr_opened(self):
        self.write_submodules(("projects/dashboard", NEW_SHA,
                               "git@github.com:example-org/dashboard.git"))
        self.run_main()
        mrs = self.gl.open_mrs()
        self.assertEqual(len(mrs), 1)
        self.assertEqual(mrs[0]["source_branch"], "build/dashboard")
        self.assertIn(NEW_SHA[:7], mrs[0]["title"])
        self.assertIn(NEW_SHA, self.gl.branch_content("build/dashboard", HOST_VARS))

    def test_the_bump_keeps_every_comment_and_every_other_line(self):
        """The whole point of this MR is that a human reads a one-line
        diff. A YAML round-trip would reformat the file and bury it."""
        self.write_submodules(("projects/dashboard", NEW_SHA,
                               "git@github.com:example-org/dashboard.git"))
        self.run_main()
        result = self.gl.branch_content("build/dashboard", HOST_VARS)
        before = MAIN_HOST_VARS.splitlines()
        after = result.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(differing), 1, "exactly one line may change")
        self.assertIn("version:", after[differing[0]])
        self.assertIn("# Kept at CasaOS's own path", result)
        self.assertIn("health_retries: 180", result)

    def test_https_remote_still_matches_an_ssh_pin(self):
        self.write_submodules(("projects/dashboard", NEW_SHA,
                               "https://github.com/example-org/dashboard"))
        self.run_main()
        self.assertEqual(len(self.gl.open_mrs()), 1)

    def test_unknown_submodule_opens_nothing(self):
        """A plain image source with no git stack anywhere is the normal
        case, not an error - and must not touch host_vars."""
        self.write_submodules(("projects/docs-site", NEW_SHA,
                               "https://gitlab.example.com/example-group/docs-site.git"))
        self.run_main()
        self.assertEqual(self.gl.open_mrs(), [])
        self.assertEqual(self.gl.branch_content("main", HOST_VARS), MAIN_HOST_VARS)

    def test_pin_already_current_opens_nothing(self):
        self.write_submodules(("projects/dashboard", OLD_SHA,
                               "git@github.com:example-org/dashboard.git"))
        out, _ = self.run_main()
        self.assertEqual(self.gl.open_mrs(), [])
        self.assertIn("already at", out)

    def test_a_second_run_updates_the_open_mr_rather_than_opening_another(self):
        row = ("projects/dashboard", NEW_SHA, "git@github.com:example-org/dashboard.git")
        self.write_submodules(row)
        self.run_main()
        self.write_submodules(row)
        out, _ = self.run_main()
        self.assertEqual(len(self.gl.open_mrs()), 1)
        self.assertIn("already up to date", out)

    def test_vault_tags_elsewhere_in_the_file_do_not_break_matching(self):
        """host_vars can carry !vault values. The matcher only reads
        these files, so an unknown tag must not make it give up and
        silently skip the bump."""
        self.gl.branches["main"][HOST_VARS] = MAIN_HOST_VARS + (
            "\nsome_secret: !vault |\n"
            "  $ANSIBLE_VAULT;1.1;AES256\n"
            "  31396233\n"
        )
        self.write_submodules(("projects/dashboard", NEW_SHA,
                               "git@github.com:example-org/dashboard.git"))
        self.run_main()
        self.assertEqual(len(self.gl.open_mrs()), 1)


class ImageInAnotherStacksComposeTest(MRTestCase):
    """A stack can hold several images, and then the compose directory
    is named after the stack, not the image - dashboard's two live in
    compose/dashboard/. Guessing compose/<image>/ missed those."""

    DASHBOARD = "compose/dashboard/docker-compose.yml"
    BODY = ("services:\n"
            "  api:\n"
            "    image: registry.example.com/library/dashboard-api:aaaaaaa\n"
            "  worker:\n"
            "    image: registry.example.com/library/dashboard-worker:aaaaaaa\n")

    def setUp(self):
        super().setUp()
        self.gl.branches["main"][self.DASHBOARD] = self.BODY

    def test_image_is_found_in_a_differently_named_directory(self):
        self.write_built(**{"dashboard-worker": "registry.example.com/library/dashboard-worker:ccccccc"})
        out, err = self.run_main()
        self.assertEqual(len(self.gl.open_mrs()), 1)
        self.assertIn("ccccccc", self.gl.branch_content("build/dashboard", self.DASHBOARD))
        self.assertNotIn("add it there first", err)

    def test_the_other_image_in_the_same_file_is_untouched(self):
        self.write_built(**{"dashboard-worker": "registry.example.com/library/dashboard-worker:ccccccc"})
        self.run_main()
        result = self.gl.branch_content("build/dashboard", self.DASHBOARD)
        self.assertIn("dashboard-api:aaaaaaa", result)

    def test_an_image_no_compose_file_references_is_still_skipped(self):
        self.write_built(**{"newthing": "registry.example.com/library/newthing:1"})
        _, err = self.run_main()
        self.assertEqual(self.gl.open_mrs(), [])
        self.assertIn("no compose file", err)



class OneMRPerStackTest(GitStackTestCase):
    """A stack's images and its checkout pin move together, so they
    belong in one merge request. Per artifact, dashboard produced three on
    three branches - two racing the same file, which is how !19 ended up
    conflicting."""

    DASHBOARD = "compose/dashboard/docker-compose.yml"

    def setUp(self):
        super().setUp()
        self.gl.branches["main"][self.DASHBOARD] = (
            "services:\n"
            "  api:\n"
            "    image: registry.example.com/library/dashboard-api:aaaaaaa\n"
            "  worker:\n"
            "    image: registry.example.com/library/dashboard-worker:aaaaaaa\n")

    def both_images_and_pin(self):
        self.write_built(**{
            "dashboard-api": "registry.example.com/library/dashboard-api:ccccccc",
            "dashboard-worker": "registry.example.com/library/dashboard-worker:ccccccc"})
        self.write_submodules(("projects/dashboard", NEW_SHA,
                               "git@github.com:example-org/dashboard.git"))
        self.run_main()

    def test_two_images_and_a_pin_make_one_mr(self):
        self.both_images_and_pin()
        mrs = self.gl.open_mrs()
        self.assertEqual(len(mrs), 1, "one stack, one MR")
        self.assertEqual(mrs[0]["source_branch"], "build/dashboard")

    def test_that_one_mr_carries_every_file(self):
        self.both_images_and_pin()
        compose = self.gl.branch_content("build/dashboard", self.DASHBOARD)
        self.assertIn("dashboard-api:ccccccc", compose)
        self.assertIn("dashboard-worker:ccccccc", compose,
                      "both images in one commit - not two that revert each other")
        self.assertIn(NEW_SHA, self.gl.branch_content("build/dashboard", HOST_VARS))

    def test_an_unrelated_stack_still_gets_its_own_mr(self):
        self.write_built(**{
            "dashboard-worker": "registry.example.com/library/dashboard-worker:ccccccc",
            "docs-site": "registry.example.com/library/docs-site:ddddddd"})
        self.run_main()
        self.assertEqual(sorted(m["source_branch"] for m in self.gl.open_mrs()),
                         ["build/dashboard", "build/docs-site"])


if __name__ == "__main__":
    unittest.main()
