"""Zipapps: `python3 -m taskmgr.pyz` builds task.pyz and taskmgr-remote.pyz that run alone."""

import os
import subprocess
import sys
import zipfile

from taskmgr import pyz
from taskmgr.records import override
from taskmgr.tests.helpers import REPO, TASK, Env


class PyzTest(Env):
    dist: str = ""
    task_pyz: str = ""
    remote_pyz: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.dist = os.path.join(self.tmp, "dist")
        p = subprocess.run(
            [sys.executable, "-m", "taskmgr.pyz", "-o", self.dist],
            cwd=REPO,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.task_pyz = os.path.join(self.dist, "task.pyz")
        self.remote_pyz = os.path.join(self.dist, "taskmgr-remote.pyz")
        # From here on nothing may import the checkout: no PYTHONPATH, cwd outside REPO.
        _ = self.env.pop("PYTHONPATH", None)
        for m in self.machines:
            m["remote_command"] = "%s %s" % (sys.executable, self.remote_pyz)
        self.write_machines()

    def run_pyz(self, pyz: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, pyz, *args],
            cwd=self.tmp,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def local_code_version(self) -> str:
        return pyz.code_version()

    def test_archives_hold_the_shipped_tree(self) -> None:
        for archive in (self.task_pyz, self.remote_pyz):
            with open(archive, "rb") as f:
                self.assertEqual(f.readline(), b"#!/usr/bin/env python3\n")
            self.assertTrue(os.access(archive, os.X_OK))
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                version = z.read(".taskmgr-code-version").decode().strip()
            self.assertIn("taskmgr/remote.py", names)
            self.assertIn("taskmgr/template/taskmgr_task.py", names)
            self.assertFalse([n for n in names if "/tests/" in n or "__pycache__" in n], names)
            self.assertEqual(version, self.local_code_version())

    def test_exit_codes_pass_through(self) -> None:
        p = self.run_pyz(self.task_pyz, "init", "bad", "--plain")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("bad_task_id", p.stderr)

    def test_remote_version_reports_the_archive(self) -> None:
        p = self.run_pyz(self.remote_pyz, "--root", self.root(), "version")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"code_version": "%s"' % self.local_code_version(), p.stdout)
        self.assertIn('"code_dir": "%s"' % self.remote_pyz, p.stdout)

    def test_imports_come_from_the_resolved_link(self) -> None:
        """Run through a symlink: the process imports from the file the link points to, so
        switching the link later (an update) cannot change the file it reads."""
        link = os.path.join(self.tmp, "current.pyz")
        os.symlink(self.remote_pyz, link)
        p = self.run_pyz(link, "--root", self.root(), "version")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"code_dir": "%s"' % os.path.realpath(self.remote_pyz), p.stdout)

    def test_local_version_matches_the_checkout(self) -> None:
        """task.pyz reports the version a checkout has, the version `task.pyz host add`
        requires of the taskmgr-remote.pyz it uploads."""
        p = self.run_pyz(self.task_pyz, "version", "--detail", "--plain")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"code_version": "%s"' % self.local_code_version(), p.stdout)
        self.assertIn('"up_to_date": true', p.stdout)
        p = self.run_pyz(self.task_pyz, "update", "--plain")
        self.assertEqual(p.returncode, 2)
        self.assertIn("not_a_checkout", p.stderr)

    def test_host_add_looks_next_to_task_pyz(self) -> None:
        """From task.pyz (also through a symlink), `host add` uploads the taskmgr-remote.pyz
        in the same directory unless --pyz is given."""
        link = os.path.join(self.tmp, "task")
        os.symlink(self.task_pyz, link)
        code = "import sys; sys.path.insert(0, %r); from taskmgr import pyz; "
        code += "print(pyz.default_remote_pyz())"
        for path in (self.task_pyz, link):
            p = subprocess.run(
                [sys.executable, "-c", code % path],
                cwd=self.tmp,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), os.path.realpath(self.remote_pyz))

    def test_init_gives_the_same_package_as_the_checkout(self) -> None:
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        p = self.run_pyz(self.task_pyz, "init", "AB-01", "--output", a, "--plain")
        self.assertEqual(p.returncode, 0, p.stderr)
        e = dict(self.env, PYTHONPATH=REPO)
        q = subprocess.run(
            [sys.executable, TASK, "init", "AB-01", "--output", b, "--plain"],
            env=e,
            capture_output=True,
        )
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(sorted(os.listdir(a)), sorted(os.listdir(b)))
        for name in os.listdir(a):
            if name == "progress.json":  # has a timestamp
                continue
            with open(os.path.join(a, name), "rb") as fa, open(os.path.join(b, name), "rb") as fb:
                self.assertEqual(fa.read(), fb.read(), name)

    def test_job_runs_through_the_archives(self) -> None:
        """Submit with task.pyz, run the daemon and the shim from taskmgr-remote.pyz."""
        pkg = self.make_pkg()
        log = open(os.path.join(self.tmp, "daemon-s1.log"), "ab")
        d = subprocess.Popen(
            [sys.executable, self.remote_pyz, "--root", self.root(), "serve"],
            cwd=self.tmp,
            env=self.env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        log.close()
        self.daemons.setdefault("s1", []).append(d)
        p = self.run_pyz(self.task_pyz, "submit", pkg, "--host", "s1", "--detail", "--plain")
        self.assertEqual(p.returncode, 0, p.stderr)
        jobid = p.stdout.split('"jobid": "')[1].split('"')[0]
        job = self.wait_state(jobid, "succeeded")
        self.assertEqual(job["state"], "succeeded", self.daemon_log())
