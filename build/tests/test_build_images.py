#!/usr/bin/env python3
"""build-images.sh decides what actually gets pushed to Harbor, and the
manual rebuild job runs the same code as the automatic one, so a wrong
answer here reaches a registry either way.

docker is stubbed. These assert the orchestration - which images, build
vs skip, which flags - not that docker itself works.
"""
import datetime
import os
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "build-images.sh")

FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/sh
    echo "$@" >> "$DOCKER_LOG"
    case "$1 $2" in
      "manifest inspect")
        for t in ${EXISTING_TAGS:-}; do [ "$t" = "$3" ] && exit 0; done
        exit 1 ;;
      "buildx inspect")
        [ "${BUILDX_OK:-0}" = "1" ] && exit 0
        exit 1 ;;
    esac
    exit 0
""")


def git(cwd, *args, allow_file=False):
    pre = ["-c", "protocol.file.allow=always"] if allow_file else []
    subprocess.run(["git", *pre, *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class BuildImagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name

        self.bin = os.path.join(self.base, "bin")
        os.makedirs(self.bin)
        docker = os.path.join(self.bin, "docker")
        with open(docker, "w") as f:
            f.write(FAKE_DOCKER)
        os.chmod(docker, 0o755)
        self.docker_log = os.path.join(self.base, "docker.log")
        open(self.docker_log, "w").close()

        # The parent repo build-images.sh runs from: scripts/ plus real
        # submodule gitlinks, since it resolves tags with
        # `git rev-parse HEAD:<submodule>`.
        self.work = os.path.join(self.base, "work")
        os.makedirs(self.work)
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "config", "user.email", "t@example.com")
        git(self.work, "config", "user.name", "t")
        os.symlink(os.path.join(REPO, "scripts"), os.path.join(self.work, "scripts"))
        with open(os.path.join(self.work, ".keep"), "w") as f:
            f.write("")
        git(self.work, "add", ".keep")
        git(self.work, "commit", "-qm", "init")

    def add_submodule(self, name, dockerfile_dirs):
        """dockerfile_dirs: [""] for a root Dockerfile, ["worker"] nested."""
        src = os.path.join(self.base, "src", name)
        os.makedirs(src)
        git(src, "init", "-q", "-b", "main")
        git(src, "config", "user.email", "t@example.com")
        git(src, "config", "user.name", "t")
        with open(os.path.join(src, "README.md"), "w") as f:
            f.write(f"# {name}\n")
        for d in dockerfile_dirs:
            full = os.path.join(src, d) if d else src
            os.makedirs(full, exist_ok=True)
            with open(os.path.join(full, "Dockerfile"), "w") as f:
                f.write("FROM scratch\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "init")
        git(self.work, "submodule", "add", "-q", src, f"projects/{name}", allow_file=True)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", f"add {name}")
        rev = subprocess.run(["git", "rev-parse", "--short", f"HEAD:projects/{name}"],
                             cwd=self.work, capture_output=True, text=True, check=True)
        return rev.stdout.strip()

    def run_script(self, submodules, expect_rc=0, **env):
        list_path = os.path.join(self.work, "subs.txt")
        with open(list_path, "w") as f:
            for s in submodules:
                f.write(f"projects/{s}\n")
        e = dict(os.environ)
        # Every knob build-images.sh or the docker stub reads has to come
        # from the test, never from the surrounding environment. The
        # suite inherits os.environ for PATH and friends, and this job
        # runs inside the very pipeline whose variables these are: a
        # pipeline started with BUILD_CACHE=false made the script take
        # the plain-build path and failed the test that asserts the
        # buildx one (pipeline 468). A test that changes answer
        # depending on how the pipeline was launched is worse than no
        # test - it fails on the runs you most want it to pass on.
        for knob in ("BUILD_CACHE", "FORCE_REBUILD", "DOCKER",
                     "EXISTING_TAGS", "BUILDX_OK"):
            e.pop(knob, None)
        e.update({
            "PATH": self.bin + os.pathsep + e["PATH"],
            "DOCKER_LOG": self.docker_log,
            "HARBOR_URL": "registry.example.com",
            "HARBOR_PROJECT": "library",
        })
        e.update({k: str(v) for k, v in env.items()})
        proc = subprocess.run(["sh", SCRIPT, "subs.txt"], cwd=self.work,
                              capture_output=True, text=True, env=e)
        self.assertEqual(proc.returncode, expect_rc,
                         f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def docker_calls(self):
        with open(self.docker_log) as f:
            return [l.rstrip("\n") for l in f]

    def built_env(self):
        p = os.path.join(self.work, "built_images.env")
        if not os.path.exists(p):
            return {}
        out = {}
        for line in open(p):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                out[k] = v
        return out

    def built_tags(self):
        """Just the NAME_IMAGE half, so a test about which tags got built
        doesn't also have to restate every image's submodule."""
        return {k: v for k, v in self.built_env().items() if k.endswith("_IMAGE")}

    def built_submodules(self):
        p = os.path.join(self.work, "built_submodules.tsv")
        if not os.path.exists(p):
            return []
        return [tuple(l.rstrip("\n").split("\t")) for l in open(p) if l.strip()]

    # --- refusing to start ------------------------------------------------

    def test_missing_harbor_url_is_fatal(self):
        self.add_submodule("docs-site", [""])
        proc = self.run_script(["docs-site"], expect_rc=1, HARBOR_URL="")
        self.assertIn("HARBOR_URL is empty", proc.stderr)

    # --- nothing to do ----------------------------------------------------

    def test_empty_submodule_list_is_a_clean_noop(self):
        self.run_script([])
        self.assertEqual(self.built_env(), {})
        self.assertEqual(self.docker_calls(), [])

    def test_submodule_with_no_dockerfile_builds_nothing(self):
        self.add_submodule("docs-only", [])
        self.run_script(["docs-only"])
        self.assertEqual(self.built_env(), {})

    # --- the submodule record (git-stack pins) -----------------------------

    def test_submodule_with_no_dockerfile_is_still_recorded(self):
        """A git stack pins a commit, not an image, so a submodule that
        produces no image at all still has to reach open_deploy_mr.py -
        otherwise its pin could never be bumped."""
        self.add_submodule("docs-only", [])
        self.run_script(["docs-only"])
        rows = self.built_submodules()
        self.assertEqual(len(rows), 1)
        path, sha, url = rows[0]
        self.assertEqual(path, "projects/docs-only")
        self.assertEqual(len(sha), 40, "must be the full SHA, not the short rev used for tags")
        self.assertTrue(url.endswith("src/docs-only"), url)

    def test_recorded_sha_is_the_full_form_of_the_image_tag(self):
        rev = self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"])
        (_, sha, _), = self.built_submodules()
        self.assertTrue(sha.startswith(rev), f"{sha} should start with tag rev {rev}")

    def test_empty_list_writes_an_empty_submodule_record(self):
        """Not just absent: open_deploy_mr.py treats a missing file and an
        empty one the same, but a stale one left from a previous job on a
        reused workspace would bump a pin nothing built this run."""
        self.run_script([])
        self.assertEqual(self.built_submodules(), [])
        self.assertTrue(os.path.exists(os.path.join(self.work, "built_submodules.tsv")))

    # --- the normal path ---------------------------------------------------

    def test_builds_pushes_and_records_the_tag(self):
        rev = self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"])
        tag = f"registry.example.com/library/docs-site:{rev}"
        self.assertEqual(self.built_tags(), {"docs-site_IMAGE": tag})
        self.assertTrue(any(c.startswith("build ") and tag in c for c in self.docker_calls()))
        self.assertIn(f"push {tag}", self.docker_calls())

    def test_build_always_pulls_its_base(self):
        """The CVE-fix rebuild this repo mostly exists for is exactly
        what a cached base layer silently undoes."""
        self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"])
        build = [c for c in self.docker_calls() if c.startswith("build ")][0]
        self.assertIn("--pull", build)

    def test_multi_image_submodule_builds_each(self):
        rev = self.add_submodule("dashboard", ["api", "worker"])
        self.run_script(["dashboard"])
        self.assertEqual(self.built_tags(), {
            "dashboard-api_IMAGE": f"registry.example.com/library/dashboard-api:{rev}",
            "dashboard-worker_IMAGE": f"registry.example.com/library/dashboard-worker:{rev}",
        })

    # --- tag immutability ---------------------------------------------------

    def test_existing_tag_is_not_rebuilt_but_is_still_reported(self):
        """Skipping the build must not skip the compose bump: a retried
        job still needs the MR opened for the image that is already
        sitting in Harbor."""
        rev = self.add_submodule("docs-site", [""])
        tag = f"registry.example.com/library/docs-site:{rev}"
        self.run_script(["docs-site"], EXISTING_TAGS=tag)
        self.assertEqual(self.built_tags(), {"docs-site_IMAGE": tag})
        self.assertFalse([c for c in self.docker_calls() if c.startswith("build ")])

    def test_force_rebuild_overwrites_and_says_so(self):
        rev = self.add_submodule("docs-site", [""])
        tag = f"registry.example.com/library/docs-site:{rev}"
        proc = self.run_script(["docs-site"], EXISTING_TAGS=tag, FORCE_REBUILD="true")
        self.assertTrue([c for c in self.docker_calls() if c.startswith("build ")])
        self.assertIn("REBUILDING ANYWAY", proc.stdout)

    def test_force_rebuild_is_off_by_default(self):
        rev = self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"], EXISTING_TAGS=f"registry.example.com/library/docs-site:{rev}")
        self.assertFalse([c for c in self.docker_calls() if c.startswith("build ")])

    # --- layer cache ---------------------------------------------------------

    def test_ambient_build_cache_does_not_leak_into_a_test(self):
        """Regression guard for pipeline 468: these tests run inside the
        pipeline whose CI/CD variables share these names, so an
        inherited BUILD_CACHE=false silently rewrote what the script
        did under test."""
        self.addCleanup(os.environ.pop, "BUILD_CACHE", None)
        os.environ["BUILD_CACHE"] = "false"
        self.add_submodule("docs-site", [""])
        proc = self.run_script(["docs-site"], BUILDX_OK="1")
        self.assertIn("layer cache: buildx registry cache", proc.stdout)

    def test_ambient_force_rebuild_does_not_leak_into_a_test(self):
        self.addCleanup(os.environ.pop, "FORCE_REBUILD", None)
        os.environ["FORCE_REBUILD"] = "true"
        rev = self.add_submodule("docs-site", [""])
        proc = self.run_script(["docs-site"],
                               EXISTING_TAGS=f"registry.example.com/library/docs-site:{rev}")
        self.assertIn("already in Harbor, skipping build", proc.stdout)

    def test_buildx_path_uses_a_registry_cache_and_pushes_directly(self):
        self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"], BUILDX_OK="1")
        build = [c for c in self.docker_calls() if c.startswith("buildx build")]
        self.assertEqual(len(build), 1)
        self.assertIn("--cache-from type=registry,ref=registry.example.com/library/docs-site:buildcache", build[0])
        self.assertIn("mode=max", build[0])
        self.assertIn("--push", build[0])
        self.assertIn("--pull", build[0])
        self.assertFalse([c for c in self.docker_calls() if c.startswith("push ")],
                         "buildx --push already pushed it")

    def test_missing_buildx_falls_back_instead_of_failing(self):
        """A cache that isn't available should make builds slow, never
        broken - the plain path is the one verified on build-runner."""
        rev = self.add_submodule("docs-site", [""])
        proc = self.run_script(["docs-site"], BUILDX_OK="0")
        self.assertIn("layer cache: none", proc.stdout)
        self.assertIn(f"push registry.example.com/library/docs-site:{rev}", self.docker_calls())

    def test_build_cache_can_be_turned_off_explicitly(self):
        self.add_submodule("docs-site", [""])
        proc = self.run_script(["docs-site"], BUILDX_OK="1", BUILD_CACHE="false")
        self.assertIn("layer cache: none", proc.stdout)
        self.assertFalse([c for c in self.docker_calls() if c.startswith("buildx build")])

    def _assert_cache_bust(self, build):
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        self.assertIn(f"--build-arg APT_REFRESH={today}", build)
        self.assertIn(f"--build-arg APK_REFRESH={today}", build)

    def test_cached_path_still_busts_the_upgrade_layer(self):
        """The one that needs it: --cache-from would otherwise restore
        the `apt-get upgrade` layer verbatim, and the rebuild would ship
        the same packages it existed to replace - dashboard-api
        :4f6d43c, 15 fixable Debian findings after a "successful"
        rebuild. A date makes the key move daily without giving up
        same-day cache hits."""
        self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"], BUILDX_OK="1")
        self._assert_cache_bust(
            [c for c in self.docker_calls() if c.startswith("buildx build")][0])

    def test_plain_path_passes_the_same_args(self):
        """Two build paths that answer differently is the drift this
        script was lifted out of .gitlab-ci.yml to avoid."""
        self.add_submodule("docs-site", [""])
        self.run_script(["docs-site"], BUILDX_OK="0")
        self._assert_cache_bust(
            [c for c in self.docker_calls() if c.startswith("build ")][0])


if __name__ == "__main__":
    unittest.main()
