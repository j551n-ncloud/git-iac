#!/usr/bin/env python3
"""check_image_cves.py decides which artifact in a Harbor repository is
"the image". Picking the wrong one reports on something nobody deploys.
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "check_image_cves", os.path.join(REPO, "scripts", "check_image_cves.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kw):
        return FakeResp(self.payload)


def art(tags, total=0, fixable=0, status="Success"):
    return {
        "tags": [{"name": t} for t in tags],
        "scan_overview": {"mime": {"scan_status": status,
                                   "summary": {"total": total, "fixable": fixable,
                                               "summary": {}}}},
    }


class LatestArtifactTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def pick(self, artifacts):
        self.mod.SESSION = FakeSession(artifacts)
        return self.mod.latest_artifact("h", "p", None, "img")

    def test_buildcache_is_skipped_for_the_real_image(self):
        """buildx pushes its cache as a tag in the same repo, often
        newest. Checking it reports 'no completed scan' for an image
        that has one."""
        got = self.pick([art(["buildcache"]), art(["4c8104d"], total=155)])
        self.assertEqual([t["name"] for t in got["tags"]], ["4c8104d"])

    def test_a_normal_repo_still_takes_the_newest(self):
        got = self.pick([art(["7df2467"]), art(["94bdf0c"])])
        self.assertEqual([t["name"] for t in got["tags"]], ["7df2467"])

    def test_untagged_artifacts_are_skipped(self):
        got = self.pick([art([]), art(["1.2.3"])])
        self.assertEqual([t["name"] for t in got["tags"]], ["1.2.3"])

    def test_a_repo_with_only_cache_yields_nothing(self):
        self.assertIsNone(self.pick([art(["buildcache"])]))


if __name__ == "__main__":
    unittest.main()
