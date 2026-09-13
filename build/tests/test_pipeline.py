#!/usr/bin/env python3
"""Guards on .gitlab-ci.yml itself. Nothing here runs a job - these are
assertions about properties that are easy to drop in an edit and whose
loss is invisible until something ships wrong.
"""
import os
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # GitLab merges `include: local:` files into one document before
        # anything else is resolved, so the tests have to as well -
        # otherwise a job moved into an included file silently stops
        # being covered by every assertion here, which is worse than
        # having no assertion at all.
        with open(os.path.join(REPO, ".gitlab-ci.yml")) as f:
            cls.doc = yaml.safe_load(f)
        for inc in cls.doc.pop("include", []) or []:
            path = inc["local"] if isinstance(inc, dict) else inc
            with open(os.path.join(REPO, path.lstrip("/"))) as f:
                included = yaml.safe_load(f) or {}
            overlap = set(included) & set(cls.doc)
            assert not overlap, f"{path} redefines {overlap}"
            cls.doc.update(included)
        cls.jobs = {k: v for k, v in cls.doc.items()
                    if isinstance(v, dict) and not k.startswith(".")
                    and k not in ("workflow", "default", "variables", "stages")}

    def resolved(self, name):
        """GitLab merges `extends` parents left to right, child last.
        Without resolving it here, every assertion below would only see
        whichever half of a job happens to be written inline."""
        job = self.doc[name]
        parents = job.get("extends") or []
        if isinstance(parents, str):
            parents = [parents]
        merged = {}
        for p in parents:
            merged.update(self.resolved(p))
        merged.update({k: v for k, v in job.items() if k != "extends"})
        return merged

    def shell(self, name, key="script"):
        """Shell only. These jobs carry long comments that deliberately
        name the commands they avoid (`system prune`, `-a`), so matching
        raw text would flag the explanation as the thing it warns about."""
        lines = "\n".join(self.resolved(name).get(key, [])).splitlines()
        return "\n".join(l for l in lines if not l.lstrip().startswith("#"))

    # --- pipeline shape ----------------------------------------------------

    def test_yaml_parses_and_has_the_expected_jobs(self):
        self.assertEqual(
            sorted(self.jobs),
            ["build-and-push", "cve-scan", "detect-changes", "open-deploy-mr",
             "rebuild", "submodule-check", "test"])

    def test_only_the_cve_scan_schedule_can_create_a_pipeline(self):
        """This replaced an assertion that NO schedule could create a
        pipeline. The reason that rule existed is unchanged - a timer
        must never push images or open merge requests on services/deploy
        - so the allowance is narrowed to one named task, and every
        other schedule is still refused."""
        rules = self.doc["workflow"]["rules"]
        allow = rules[0]
        self.assertIn("schedule", allow["if"])
        for task in ("cve-scan", "submodule-check"):
            self.assertIn(task, allow["if"])
        self.assertNotIn("when", allow, "this rule must permit, not defer")

        deny = rules[1]
        self.assertIn("schedule", deny["if"])
        self.assertEqual(deny["when"], "never",
                         "every other schedule must still be refused")
        self.assertEqual(rules[-1], {"when": "never"})

    def test_no_publishing_job_runs_on_a_schedule(self):
        """The workflow allowance above is not enough on its own: a
        scheduled pipeline still has CI_COMMIT_BRANCH == main, so a job
        whose only rule is `branch == main` would run on the timer. Every
        job that pushes to Harbor or writes to services/deploy has to
        refuse a schedule itself."""
        for name in ("build-and-push", "rebuild", "open-deploy-mr", "detect-changes"):
            rules = self.resolved(name).get("rules") or []
            guard = [r for r in rules
                     if "schedule" in str(r.get("if", "")) and r.get("when") == "never"]
            self.assertTrue(guard, f"{name} has no 'schedule -> never' guard")
            self.assertIs(rules[0], guard[0],
                          f"{name}'s schedule guard must come first, "
                          "or an earlier matching rule wins")

    def test_scheduled_jobs_only_run_on_their_own_schedule(self):
        """The inverse: they must not fire on a push or a merge request,
        where they would open issues off whatever is on the branch."""
        for name in ("cve-scan", "submodule-check"):
            rules = self.resolved(name)["rules"]
            self.assertEqual(len(rules), 1, name)
            self.assertIn("schedule", rules[0]["if"], name)
            self.assertIn(name, rules[0]["if"], name)

    def test_scheduled_jobs_publish_nothing(self):
        """They read and write GitLab issues. If one ever grows a docker
        push or an MR call, the schedule allowance stops being safe."""
        for name in ("cve-scan", "submodule-check"):
            script = self.shell(name) + self.shell(name, "before_script")
            for forbidden in ("docker push", "docker build", "buildx", "open_deploy_mr"):
                self.assertNotIn(forbidden, script, f"{name}: {forbidden}")

    def test_every_job_runs_on_the_build_runner(self):
        """This pipeline needs LAN access to Harbor and a privileged
        host for dind. Landing on services/deploy's deploy-runner instead
        fails in ways that look like anything but a missing tag."""
        self.assertEqual(self.doc["default"]["tags"], ["build"])
        for name in self.jobs:
            self.assertEqual(self.resolved(name).get("tags", ["build"]), ["build"], name)

    def test_only_the_test_job_runs_on_merge_requests(self):
        """An MR must not publish an image. `test` touches no registry
        and no API, which is what makes it safe there - it is the only
        job whose rules do not require the default branch."""
        for name in self.jobs:
            rules = self.resolved(name)["rules"]
            if name == "test":
                # Has rules only to sit out the CVE schedule; nothing in
                # them may restrict it to main, or MRs stop being tested.
                self.assertFalse(any("CI_DEFAULT_BRANCH" in str(r.get("if", ""))
                                     for r in rules))
                continue
            gating = [r for r in rules if r.get("when") != "never"]
            self.assertTrue(gating, name)
            self.assertTrue(
                all("CI_DEFAULT_BRANCH" in r["if"] or "SCHEDULE_TASK" in r["if"]
                    for r in gating), name)

    # --- serialisation -----------------------------------------------------

    def test_jobs_that_write_somewhere_are_serialised(self):
        """Two pipelines racing the same registry, or the same
        build/<name> branch on services/deploy."""
        for name in ("build-and-push", "rebuild", "open-deploy-mr"):
            self.assertIn("resource_group", self.resolved(name), name)

    def test_nothing_is_interruptible(self):
        """The reflex fix for a queue, and wrong here: detect-changes
        only diffs its own push range, so cancelling a superseded
        pipeline means the submodule bump it carried is never built by
        anything, and the next pipeline's range starts after it."""
        for name in self.jobs:
            self.assertNotIn("interruptible", self.resolved(name), name)

    # --- the build itself ---------------------------------------------------

    def test_both_build_jobs_run_the_same_script(self):
        """The manual job exists to rebuild exactly what the automatic
        one would have. Two inline copies of the loop would drift."""
        for name in ("build-and-push", "rebuild"):
            self.assertIn("scripts/build-images.sh", self.shell(name), name)

    def test_a_manual_rebuild_exists_and_is_opt_in(self):
        """Without it the only way to rebuild anything is an empty
        submodule bump, which lies in the history of the repo you push
        it to."""
        rules = self.resolved("rebuild")["rules"]
        self.assertTrue(any(r.get("when") == "manual" for r in rules))
        self.assertEqual(self.resolved("rebuild")["variables"]["FORCE_REBUILD"], "false")

    def test_prune_runs_even_when_the_build_failed(self):
        """A failed build is exactly when a pile of half-finished
        layers gets left on the runner."""
        after = self.shell("build-and-push", "after_script")
        self.assertIn("docker image prune -f", after)
        for bad in ("prune -a", "system prune"):
            self.assertNotIn(bad, after)

    def test_built_images_is_a_plain_artifact_not_dotenv(self):
        """dotenv key validation rejects hyphens, and every image name
        here has one (dashboard-api)."""
        for name in ("build-and-push", "rebuild"):
            artifacts = self.resolved(name)["artifacts"]
            self.assertIn("built_images.env", artifacts["paths"], name)
            self.assertNotIn("reports", artifacts, name)

    def test_the_git_stack_handover_file_is_published_too(self):
        """open_deploy_mr.py bumps a `version:` pin from this, so a job
        that publishes only built_images.env silently stops bumping
        every git stack while still looking green."""
        for name in ("build-and-push", "rebuild"):
            self.assertIn("built_submodules.tsv", self.resolved(name)["artifacts"]["paths"], name)

    def test_no_floating_tags_on_anything_this_pipeline_builds(self):
        """Same discipline services/deploy applies to every compose pin.
        Job images are pinned by policy elsewhere; this is about what
        gets pushed to Harbor."""
        with open(os.path.join(REPO, "scripts", "build-images.sh")) as f:
            body = "\n".join(l for l in f if not l.lstrip().startswith("#"))
        self.assertNotIn(":latest", body)


if __name__ == "__main__":
    unittest.main()
