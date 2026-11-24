"""Server features: a package using a task.json feature the server's code lacks is refused
before the upload, and task.json keys the code does not know are errors, never ignored."""

import json
import os
from typing import cast

from taskmgr import package
from taskmgr.common import FEATURES
from taskmgr.records import JsonDict, JsonValue
from taskmgr.tests.helpers import Env


class FeaturesTest(Env):
    def test_every_reply_reports_the_features(self) -> None:
        rc, out, _ = self.remote("version")
        self.assertEqual(rc, 0)
        self.assertEqual(self.strs(out, "features"), sorted(FEATURES))
        rc, out, _ = self.remote("get", "jobs")
        self.assertEqual(self.strs(out, "features"), sorted(FEATURES))
        ping = self.ok("ping", "host", "s1")
        self.assertEqual(self.strs(ping, "hosts", 0, "features"), sorted(FEATURES))
        version = self.ok("get", "version", "--host", "s1")
        self.assertEqual(self.strs(version, "hosts", 0, "features"), sorted(FEATURES))
        # Command results other than the server reports do not repeat them.
        j = self.submit(self.make_pkg())
        self.assertNotIn("features", self.obj(self.ok("cancel", "job", j, "--host", "s1")))

    def test_validate_lists_the_features_a_package_needs(self) -> None:
        out = self.ok("validate", "package", self.make_pkg())
        self.assertEqual(self.strs(out, "features"), [])
        pre = self.make_pkg(extra={"precompile": {"argv": ["true"]}})
        self.assertEqual(self.strs(self.ok("validate", "package", pre), "features"), ["precompile"])

    def test_submit_refuses_a_server_without_the_feature(self) -> None:
        pkg = self.make_pkg(extra={"precompile": {"argv": ["true"]}})
        old = {"TASKMGR_TEST_FEATURES": "-"}  # a server older than the feature list
        rc, out, err = self.task("create", "job", pkg, "--host", "s1", env=old)
        self.assertEqual(rc, 2, err)
        e = self.obj(out, "error")
        self.assertEqual(e["code"], "unsupported_feature")
        msg = self.text(e, "message")
        self.assertIn("server s1 runs", msg)
        self.assertIn("without precompile support", msg)
        self.assertIn("run `task update host s1`", msg)
        self.assertEqual(self.strs(e, "details", "missing"), ["precompile"])
        self.assertEqual(self.jobids(), set())  # nothing was uploaded
        incoming = os.path.join(self.root(), "incoming")
        self.assertFalse(os.path.isdir(incoming) and os.listdir(incoming))
        # A server that has the feature takes it; a package without features needs none.
        _ = self.submit(pkg)
        rc, _, err = self.task("create", "job", self.make_pkg(), "--host", "s1", env=old)
        self.assertEqual(rc, 0, err)

    def test_unknown_keys_are_errors_locally_and_on_the_server(self) -> None:
        cases: dict[str, tuple[JsonDict, str]] = {
            "top": ({"precomplie": {"argv": ["true"]}}, "precomplie"),
            "nested": ({"entry": {"argv": ["true"], "cwd": "/"}}, "entry.cwd"),
            "nested2": ({"environment": {"venvv": "/v"}}, "environment.venvv"),
        }
        for name, (extra, key) in cases.items():
            with self.subTest(name):
                pkg = self.make_pkg(extra=extra)
                rc, out, _ = self.task("validate", "package", pkg)
                self.assertEqual(rc, 1)
                errs = [e for e in self.objs(out, "errors") if e["code"] == "unknown_key"]
                self.assertEqual(len(errs), 1, out)
                self.assertIn(repr(key), self.text(errs[0], "message"))
                # The server checks it too (an archive sent straight to `receive`).
                tar = os.path.join(self.tmp, "%s.tar.gz" % name)
                package.pack_dir(pkg, tar)
                with open(tar, "rb") as f:
                    rc, out, _ = self.remote("create", "job", stdin=f)
                self.assertEqual(rc, 2)
                self.assertEqual(self.get(out, "error", "code"), "invalid_package")
                codes = [self.get(d, "code") for d in self.arr(out, "error", "details")]
                self.assertIn("unknown_key", codes)
        # x-* keys are annotations, at any level.
        pkg = self.make_pkg(extra={"x-owner": "me", "entry": {"argv": ["true"], "x-why": "t"}})
        _ = self.ok("validate", "package", pkg)

    def test_template_keys_are_known(self) -> None:
        """Every key of the template's task.json is in package.TASK_KEYS."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "template", "task.json")) as f:
            task = self.obj(cast(JsonValue, json.load(f)))
        self.assertLessEqual(set(task), set(package.TASK_KEYS[""]))
