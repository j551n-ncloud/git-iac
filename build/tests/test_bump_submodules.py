#!/usr/bin/env python3
"""bump_submodules.py moves this repo's submodule pointers, on a timer,
with nobody watching. The pointer is what every image here is built
from, so the cases that matter are the ones that produce a *plausible*
merge request whose merge button does the wrong thing - the shape of
the incident this script replaced (a revert that pinned
a SHA that had gone stale inside an open issue).
"""
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_gitlab import FakeGitLab  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUB = "projects/docs-site"
URL = "https://gitlab.example.com/example-group/docs-site.git"
PINNED = "65d8c090e72b23a7c2875fe66f735c5412532fe9"
HEAD = "d3bd255cc9a06b4d0c4f8266bbd47e0646ceb0f8"
NEWER = "f00dcafe1234567890abcdef1234567890abcdef"


def load_module():
    """Fresh import per test - the module keeps a module-level SESSION."""
    spec = importlib.util.spec_from_file_location(
        "bump_submodules", os.path.join(REPO, "scripts", "bump_submodules.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BumpTestCase(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))
        os.environ["CI_API_V4_URL"] = "https://gitlab.example.com/api/v4"
        os.environ["BUILD_API_TOKEN"] = "token"
        os.environ["CVE_PROJECT_PATH"] = "services/build"

        self.mod = load_module()
        # The three git-backed lookups are stubbed: this suite is about
        # what gets committed and opened, not about parsing .gitmodules.
        self.mod.submodules = lambda: [(SUB, URL)]
        self.mod.pinned_sha = lambda path: PINNED
        self.upstream = ("main", HEAD)
        self.mod.remote_default = lambda url: self.upstream

        self.gl = FakeGitLab({"README.md": "# build\n"}, {SUB: PINNED})
        self.mod.SESSION = self.gl

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.mod.main()
        return out.getvalue(), err.getvalue()

    def submodule_writes(self):
        return [c for c in self.gl.calls
                if c[0] == "PUT" and c[1].startswith("repository/submodules/")]


class OpensTheBumpTest(BumpTestCase):
    def test_a_stale_pointer_becomes_an_mr_on_its_own_branch(self):
        self.run_main()

        mrs = self.gl.open_mrs()
        self.assertEqual(len(mrs), 1)
        self.assertEqual(mrs[0]["source_branch"], "bump/docs-site")
        self.assertEqual(mrs[0]["target_branch"], "main")
        self.assertIn(HEAD[:7], mrs[0]["title"])
        self.assertEqual(self.gl.gitlink("bump/docs-site", SUB), HEAD)

    def test_main_is_never_written_to(self):
        """The whole reason this opens an MR instead of committing. A
        push to main here builds images, pushes them to Harbor and opens
        a merge request against services/deploy - none of which may
        happen on a timer."""
        before = dict(self.gl.gitlinks["main"]), dict(self.gl.branches["main"])
        self.run_main()
        self.assertEqual((self.gl.gitlinks["main"], self.gl.branches["main"]), before)
        for method, tail in self.gl.calls:
            self.assertNotEqual((method, tail), ("POST", "repository/commits"))

    def test_the_mr_body_links_the_compare_view(self):
        self.run_main()
        body = self.gl.open_mrs()[0]["description"]
        self.assertIn(f"https://gitlab.example.com/example-group/docs-site/-/compare/{PINNED}...{HEAD}", body)
        self.assertIn(PINNED[:12], body)
        self.assertIn(HEAD[:12], body)


class CurrentPointerTest(BumpTestCase):
    def test_nothing_is_opened_when_the_pointer_is_current(self):
        self.upstream = ("main", PINNED)
        out, _ = self.run_main()
        self.assertEqual(self.gl.open_mrs(), [])
        self.assertIn("current", out)
        self.assertIn("0 submodule(s) behind", out)

    def test_a_hand_bump_closes_the_open_mr_and_deletes_its_branch(self):
        """Someone ran sync-submodule.sh while an MR sat open. Its diff
        is now a no-op at best and a revert at worst, and the merge
        button is still there."""
        self.run_main()
        iid = self.gl.open_mrs()[0]["iid"]

        self.mod.pinned_sha = lambda path: HEAD      # the hand bump landed
        self.upstream = ("main", HEAD)
        self.run_main()

        self.assertEqual(self.gl.open_mrs(), [])
        self.assertNotIn("bump/docs-site", self.gl.branches)
        self.assertIn("already pinned", self.gl.notes[iid][0])


class IdempotenceTest(BumpTestCase):
    def test_a_second_run_does_not_open_a_second_mr_or_recommit(self):
        """It runs daily against a pointer that stays stale until someone
        merges. A duplicate MR per day, or a stream of empty commits onto
        one an author is reviewing, is how that goes wrong."""
        self.run_main()
        self.assertEqual(len(self.submodule_writes()), 1)

        out, _ = self.run_main()
        self.assertEqual(len(self.gl.open_mrs()), 1)
        self.assertEqual(len(self.submodule_writes()), 1)
        self.assertIn("already at", out)

    def test_the_open_mr_advances_when_upstream_moves_again(self):
        self.run_main()
        iid = self.gl.open_mrs()[0]["iid"]

        self.upstream = ("main", NEWER)
        self.run_main()

        self.assertEqual(len(self.gl.open_mrs()), 1)
        self.assertEqual(self.gl.open_mrs()[0]["iid"], iid)
        self.assertEqual(self.gl.gitlink("bump/docs-site", SUB), NEWER)
        self.assertIn(NEWER[:7], self.gl.open_mrs()[0]["title"])


class StaleBranchTest(BumpTestCase):
    def test_a_branch_left_by_a_closed_mr_is_cut_again_from_main(self):
        """GitLab's remove_source_branch only fires on merge, so a CLOSED
        MR leaves its branch behind pointing at an older main. Committing
        onto it shows the reviewer every unrelated change main has made
        since as though this MR introduced it - and the only thing a
        human does here is read that diff. open_deploy_mr.py learned the
        same lesson in 09b2a49."""
        self.gl.branches["bump/docs-site"] = {"README.md": "# stale\n"}
        self.gl.gitlinks["bump/docs-site"] = {SUB: "0" * 40}
        self.gl.mrs.append({
            "iid": 1, "source_branch": "bump/docs-site", "target_branch": "main",
            "title": "old", "description": "", "state": "closed"})

        out, _ = self.run_main()

        self.assertIn("recreating it from main", out)
        self.assertEqual(self.gl.branch_content("bump/docs-site", "README.md"), "# build\n")
        self.assertEqual(self.gl.gitlink("bump/docs-site", SUB), HEAD)
        self.assertEqual(len(self.gl.open_mrs()), 1)


class UnreadableRemoteTest(BumpTestCase):
    def test_a_remote_that_cannot_be_read_is_skipped_not_called_current(self):
        """A network blip must not read as 'up to date', and must not
        close an MR that is still needed."""
        self.run_main()
        self.upstream = ("", "")
        out, err = self.run_main()

        self.assertIn("not assuming current", err)
        self.assertEqual(len(self.gl.open_mrs()), 1)
        self.assertNotIn("current", out)


class WebUrlTest(BumpTestCase):
    def test_https_remotes_get_a_link(self):
        self.assertEqual(self.mod.web_url(URL), "https://gitlab.example.com/example-group/docs-site")
        self.assertEqual(self.mod.web_url("https://gitlab.example.com/example-group/docs-site"),
                         "https://gitlab.example.com/example-group/docs-site")

    def test_ssh_remotes_get_no_link_rather_than_a_wrong_one(self):
        """dashboard's remote is ssh.gitlab.example.com:2224, a different host
        than the web UI's gitlab.example.com. Deriving a link from it would
        give a confident 404 in an MR a human is meant to trust."""
        for url in ("ssh://git@ssh.gitlab.example.com:2224/example-group/dashboard.git",
                    "git@gitlab.example.com:example-group/dashboard.git"):
            self.assertEqual(self.mod.web_url(url), "", url)

    def test_a_missing_link_still_leaves_a_usable_mr(self):
        self.mod.submodules = lambda: [("projects/dashboard", "ssh://git@h:2224/example-group/dashboard.git")]
        self.gl.gitlinks["main"]["projects/dashboard"] = PINNED
        self.run_main()
        body = self.gl.open_mrs()[0]["description"]
        self.assertNotIn("/-/compare/", body)
        self.assertIn(HEAD[:12], body)


if __name__ == "__main__":
    unittest.main()
