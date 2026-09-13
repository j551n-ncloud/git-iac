#!/usr/bin/env python3
"""discover-images.sh is the single source of truth for what this
pipeline builds and pushes to Harbor, and nothing exercised it before
this file existed. Every case here is a way a wrong answer turns into
either a missing image or an unintended push.

Fixtures are real git repos, because a submodule always is one by the
time either caller reaches this script (add-submodule.sh runs right
after `git submodule add`, build-and-push after `git submodule update`).
"""
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "discover-images.sh")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class DiscoverImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.root)
        self.addCleanup(self.tmp.cleanup)

    def make_submodule(self, name, files, commit=True):
        """files: {relpath: content}. Returns the submodule path."""
        path = os.path.join(self.root, name)
        os.makedirs(path)
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "t")
        for rel, content in files.items():
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        if commit:
            git(path, "add", "-A")
            git(path, "commit", "-qm", "init")
        return path

    def run_script(self, arg, expect_rc=0):
        proc = subprocess.run(["sh", SCRIPT, arg], cwd=self.tmp.name,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, expect_rc,
                         f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def names(self, arg):
        out = self.run_script(arg).stdout
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    def rows(self, arg):
        out = self.run_script(arg).stdout
        return [tuple(line.split()) for line in out.splitlines() if line.strip()]

    # --- the naming convention README documents -------------------------

    def test_root_dockerfile_uses_submodule_name(self):
        self.make_submodule("docs-site", {"Dockerfile": "FROM scratch\n"})
        self.assertEqual(self.rows("projects/docs-site"),
                         [("docs-site", "projects/docs-site", "projects/docs-site")])

    def test_nested_dockerfile_is_submodule_dash_subpath(self):
        self.make_submodule("dashboard", {
            "api/Dockerfile": "FROM scratch\n",
            "worker/Dockerfile": "FROM scratch\n",
        })
        self.assertEqual(self.names("projects/dashboard"),
                         ["dashboard-api", "dashboard-worker"])

    def test_deeply_nested_joins_with_dashes(self):
        self.make_submodule("mono", {"a/b/Dockerfile": "FROM scratch\n"})
        self.assertEqual(self.names("projects/mono"), ["mono-a-b"])

    # --- things that must NOT become an image ---------------------------

    def test_untracked_dockerfile_is_ignored(self):
        """An uncommitted Dockerfile is not something a pipeline run
        should push on your behalf - CI only ever sees committed files,
        so a local one must not change what discovery reports either."""
        path = self.make_submodule("sub", {"Dockerfile": "FROM scratch\n"})
        with open(os.path.join(path, "scratchpad", "Dockerfile")
                  .replace("scratchpad/", ""), "a"):
            pass
        os.makedirs(os.path.join(path, "experiment"))
        with open(os.path.join(path, "experiment", "Dockerfile"), "w") as f:
            f.write("FROM scratch\n")
        self.assertEqual(self.names("projects/sub"), ["sub"])

    def test_gitignored_dependency_tree_is_ignored(self):
        """node_modules is the live case: projects/docs-site has one on
        disk today. A dependency shipping its own Dockerfile must never
        become an image this pipeline builds and pushes."""
        path = self.make_submodule("docs-site", {
            "Dockerfile": "FROM scratch\n",
            ".gitignore": "node_modules/\n",
        })
        dep = os.path.join(path, "node_modules", "some-dep")
        os.makedirs(dep)
        with open(os.path.join(dep, "Dockerfile"), "w") as f:
            f.write("FROM scratch\n")
        self.assertEqual(self.names("projects/docs-site"), ["docs-site"])

    def test_directory_named_dockerfile_is_not_an_image(self):
        """`find -name Dockerfile` without -type f matches a directory
        too, which would emit a row whose build context is nonsense."""
        self.make_submodule("sub", {
            "app/Dockerfile": "FROM scratch\n",
            "docs/Dockerfile/notes.md": "how the image is built\n",
        })
        self.assertEqual(self.names("projects/sub"), ["sub-app"])

    def test_file_merely_ending_in_dockerfile_is_not_an_image(self):
        """Dockerfile.build and prod.Dockerfile are not this scheme's
        convention, and a loose glob would pick them up as images whose
        context is the directory they happen to sit in."""
        self.make_submodule("sub", {
            "Dockerfile": "FROM scratch\n",
            "other/prod.Dockerfile": "FROM scratch\n",
            "other/Dockerfile.build": "FROM scratch\n",
        })
        self.assertEqual(self.names("projects/sub"), ["sub"])

    def test_vendored_nested_repo_is_skipped(self):
        """dashboard's vendored-lib is the case this guards: third-party code
        embedded in a submodule, not something to build on your behalf."""
        path = self.make_submodule("dashboard", {"worker/Dockerfile": "FROM scratch\n"})
        vendor = os.path.join(path, "vendored-lib")
        os.makedirs(vendor)
        with open(os.path.join(vendor, "Dockerfile"), "w") as f:
            f.write("FROM scratch\n")
        git(vendor, "init", "-q", "-b", "main")
        git(vendor, "config", "user.email", "t@example.com")
        git(vendor, "config", "user.name", "t")
        git(vendor, "add", "-A")
        git(vendor, "commit", "-qm", "vendored")
        self.assertEqual(self.names("projects/dashboard"), ["dashboard-worker"])

    # --- argument handling ----------------------------------------------

    def test_trailing_slash_does_not_change_the_answer(self):
        """Callers pass a path from .gitmodules today, but a trailing
        slash silently breaks the name derivation rather than failing."""
        self.make_submodule("dashboard", {"worker/Dockerfile": "FROM scratch\n"})
        self.assertEqual(self.rows("projects/dashboard/"),
                         self.rows("projects/dashboard"))

    def test_no_dockerfile_is_empty_and_successful(self):
        """add-submodule.sh relies on this: a submodule with nothing
        buildable yet is a warning, not an error."""
        self.make_submodule("empty", {"README.md": "nothing here\n"})
        self.assertEqual(self.names("projects/empty"), [])

    # --- collisions -------------------------------------------------------

    def test_colliding_names_fail_loudly(self):
        """a-b/Dockerfile and a/b/Dockerfile both derive `sub-a-b`, so
        two different build contexts would push to one Harbor tag and
        the second would silently overwrite the first."""
        self.make_submodule("sub", {
            "a-b/Dockerfile": "FROM scratch\n",
            "a/b/Dockerfile": "FROM scratch\n",
        })
        proc = self.run_script("projects/sub", expect_rc=1)
        self.assertIn("sub-a-b", proc.stderr)


if __name__ == "__main__":
    unittest.main()
