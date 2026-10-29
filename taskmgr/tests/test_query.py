"""Job discovery, status lookup across servers, --wait, logs and fetch (Sections 14, 15, 16)."""

import os
import re
import shutil
import subprocess
import sys
import threading
import time

from taskmgr.records import override
from taskmgr.tests.helpers import TASK, Env


class QueryTest(Env):
    servers: tuple[str, ...] = ("s1", "s2", "s3")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.machines[2]["enabled"] = False  # disabled servers are still queried
        self.write_machines()

    def unreachable(self, name: str, on: bool = True) -> None:
        if on:
            self.server(name)["local"] = {"fail": "ssh: connect to host %s timed out" % name}
        else:
            _ = self.server(name).pop("local", None)
        self.write_machines()

    def test_jobs_across_servers(self) -> None:
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")
        b = self.submit(self.make_pkg(task_id="BB-02"), host="s3")
        out = self.ok("jobs", "--detail")
        self.assertTrue(self.get(out, "complete"))
        self.assertEqual(self.get(out, "errors"), [])
        got = {(j["host"], j["jobid"], j["task_id"], j["state"]) for j in self.objs(out, "jobs")}
        self.assertEqual(got, {("s1", a, "AA-01", "queued"), ("s3", b, "BB-02", "queued")})
        for j in self.objs(out, "jobs"):
            self.assertIn("submitted_at", j)
            self.assertEqual((j["retry_of"], j["retried_as"]), (None, []))
            self.assertNotIn("attempt", j)
            self.assertNotIn("attempt_count", j)
        out = self.ok("jobs", "--detail", "--host", "s3")
        self.assertEqual([j["jobid"] for j in self.objs(out, "jobs")], [b])
        _ = self.ok("cancel", a, "--host", "s1")
        out = self.ok("jobs", "--detail", "--state", "cancelled")
        self.assertEqual([j["jobid"] for j in self.objs(out, "jobs")], [a])
        self.unreachable("s2")
        out = self.ok("jobs", "--detail")
        self.assertFalse(self.get(out, "complete"))
        self.assertEqual([e["host"] for e in self.objs(out, "errors")], ["s2"])
        self.assertIn("timed out", self.text(out, "errors", 0, "error"))
        self.assertEqual(len(self.arr(out, "jobs")), 2)
        self.unreachable("s1")
        self.unreachable("s3")
        rc, out, _ = self.task("jobs", "--detail")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "all_hosts_failed")

    def test_jobs_table(self) -> None:
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")  # s1 has no daemon: stays queued
        time.sleep(1.1)  # submitted_at has 1 s resolution; make the order unambiguous
        b = self.submit(self.make_pkg(task_id="BB-02"), host="s3")
        _ = self.start_daemon("s3")
        _ = self.wait_state(b, "succeeded", name="s3")
        rc, out, err = self.task("jobs")
        self.assertEqual((rc, err), (0, ""))
        assert isinstance(out, str)  # a table, not JSON
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["JOBID", "TASK_ID", "SUBMITTED", "HOST", "STATE"])
        rows = [line.split() for line in lines[1:]]
        self.assertEqual([r[0] for r in rows], [a, b])  # oldest submission first
        self.assertEqual(
            [(r[1], r[3], r[4]) for r in rows],
            [("AA-01", "s1", "queued"), ("BB-02", "s3", "succeeded")],
        )
        self.assertEqual(rows[0][2], self.job(a)["submitted_at"])
        # Columns line up: every row starts each column at the header's offset.
        starts = [lines[0].index(c) for c in ("TASK_ID", "SUBMITTED", "HOST", "STATE")]
        for line in lines[1:]:
            self.assertTrue(all(line[p - 2 : p] == "  " and line[p] != " " for p in starts))
        # Color: never on a pipe (auto), on with --color always, and the escape codes
        # do not change the layout.
        self.assertNotIn("\x1b[", out)
        rc, colored, _ = self.task("jobs", "--color", "always")
        assert isinstance(colored, str)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[1mJOBID", colored)  # bold header
        self.assertIn("\x1b[33mqueued\x1b[0m", colored)  # yellow state
        self.assertIn("\x1b[32msucceeded\x1b[0m", colored)  # green state
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", colored), out)
        rc, out2, _ = self.task("jobs", "--color", "never", env={"TERM": "xterm"})
        self.assertEqual((rc, out2), (0, out))
        rc, out, _ = self.task("jobs", "--state", "running")
        self.assertEqual((rc, out), (0, "JOBID  TASK_ID  SUBMITTED  HOST  STATE\n"))
        # Unreachable servers: still a table on stdout, a warning on stderr, exit 0.
        self.unreachable("s2")
        rc, out, err = self.task("jobs")
        assert isinstance(out, str)
        self.assertEqual((rc, len(out.splitlines())), (0, 3))
        self.assertIn("warning: s2 unreachable", err)
        # Nothing reachable: exit 2, nothing on stdout.
        self.unreachable("s1")
        self.unreachable("s3")
        rc, out, err = self.task("jobs")
        self.assertEqual((rc, out), (2, None))
        self.assertIn("no server could be queried", err)
        rc, out, err = self.task("jobs", "--host", "nope")
        self.assertEqual((rc, out), (2, None))
        self.assertIn("unknown_host", err)

    def test_status_lookup_table(self) -> None:
        j = self.submit(self.make_pkg(), host="s3")
        out = self.ok("status", j)
        self.assertEqual(
            (self.get(out, "host"), self.get(out, "state"), self.get(out, "complete")),
            ("s3", "queued", True),
        )
        self.unreachable("s1")
        rc, out, err = self.task("status", j)
        self.assertEqual(rc, 0)
        self.assertFalse(self.get(out, "complete"))
        self.assertIn("uniqueness", err)
        rc, out, _ = self.task("status", "0123456789abcdef")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "incomplete"))
        self.unreachable("s1", False)
        rc, out, _ = self.task("status", "0123456789abcdef")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "not_found"))
        # Same jobid on two servers: ambiguity.
        _ = shutil.copytree(
            self.job_dir(j, "s3"), os.path.join(self.root("s2"), "queue", j), symlinks=True
        )
        rc, out, _ = self.task("status", j)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "ambiguous"))
        self.assertEqual(sorted(self.strs(out, "error", "details", "hosts")), ["s2", "s3"])
        out = self.ok("status", j, "--host", "s2")
        self.assertEqual(self.get(out, "host"), "s2")
        rc, out, _ = self.task("status", "0123456789abcdef", "--host", "s1")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "not_found"))

    def test_wait_outcomes(self) -> None:
        _ = self.start_daemon("s1")
        ok = self.submit(
            self.make_pkg(script="sleep 1; echo '{\"outputs\": {}}' > $TASKMGR_RESULT_FILE")
        )
        rc, out, _ = self.task("status", ok, "--wait", "--poll-interval", "0.2")
        self.assertEqual(rc, 0)
        self.assertEqual(self.get(out, "_taskmgr", "status"), "succeeded")
        bad = self.submit(self.make_pkg(script="exit 2"))
        rc, out, _ = self.task("status", bad, "--host", "s1", "--wait", "--poll-interval", "0.2")
        self.assertEqual(rc, 1)
        self.assertEqual(self.get(out, "_taskmgr", "status"), "failed")
        slow = self.submit(self.make_pkg(script="sleep 30"))
        t0 = time.time()
        rc, out, _ = self.task(
            "status", slow, "--host", "s1", "--wait", "--poll-interval", "0.2", "--timeout", "1"
        )
        self.assertEqual(rc, 2)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(self.get(out, "state"), "running")
        self.assertEqual(self.get(out, "error", "code"), "wait_timeout")
        self.assertEqual(self.job(slow)["state"], "running")  # wait never changes remote state

    def test_wait_retries_connection_failures(self) -> None:
        _ = self.start_daemon("s1")
        flag = os.path.join(self.tmp, "down")
        self.server("s1")["local"] = {"fail_if_exists": flag}
        self.write_machines()
        j = self.submit(
            self.make_pkg(script="sleep 2; echo '{\"outputs\": {}}' > $TASKMGR_RESULT_FILE")
        )
        open(flag, "w").close()
        threading.Timer(1.5, lambda: os.unlink(flag)).start()
        rc, out, err = self.task(
            "status", j, "--host", "s1", "--wait", "--poll-interval", "0.2", "--retry-timeout", "30"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("retrying", err)
        # Beyond --retry-timeout it gives up with a connection error.
        j2 = self.submit(self.make_pkg(script="sleep 30"))
        open(flag, "w").close()
        rc, out, _ = self.task(
            "status", j2, "--host", "s1", "--wait", "--poll-interval", "0.2", "--retry-timeout", "1"
        )
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "connection_error"))
        # --timeout still applies while connection failures are being retried.
        t0 = time.time()
        rc, out, _ = self.task(
            "status",
            j2,
            "--host",
            "s1",
            "--wait",
            "--poll-interval",
            "0.2",
            "--retry-timeout",
            "60",
            "--timeout",
            "1",
        )
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "wait_timeout"))
        self.assertLess(time.time() - t0, 15)
        os.unlink(flag)
        self.assertEqual(self.job(j2)["state"], "running")

    def test_wait_without_host_locates_once(self) -> None:
        _ = self.start_daemon("s2")
        j = self.submit(self.make_pkg(script="sleep 1.5; exit 3"), host="s2")
        flag = os.path.join(self.tmp, "s1down")
        self.server("s1")["local"] = {"fail_if_exists": flag}
        self.write_machines()
        p = subprocess.Popen(
            [sys.executable, TASK, "status", j, "--wait", "--poll-interval", "0.2"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.8)
        open(flag, "w").close()  # s1 going down after the lookup does not matter
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1, err)

    def test_logs_and_follow(self) -> None:
        _ = self.start_daemon("s1")
        j = self.submit(
            self.make_pkg(script="for i in 1 2 3; do echo line$i; sleep 0.5; done; echo err >&2")
        )
        p = subprocess.Popen(
            [sys.executable, TASK, "logs", j, "--host", "s1", "--follow"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(out.decode(), "line1\nline2\nline3\n")
        _ = self.wait_state(j, "invalid_result")
        rc, out, _ = self.task("logs", j, "--host", "s1", "--stream", "stderr")
        self.assertEqual((rc, out), (0, "err\n"))
        rc, out, _ = self.task("logs", j, "--host", "s1", "--stream", "shim")
        self.assertIn("finalized", self.text(out))
        rc, out, _ = self.task("logs", j, "--host", "s1", "--attempt", "1")
        self.assertEqual(rc, 2)  # there are no attempts any more

    def test_logs_of_a_queued_job(self) -> None:
        q = self.submit(self.make_pkg(script="echo hello"), host="s2")  # s2: no daemon yet
        rc, out, err = self.task("logs", q, "--host", "s2")
        self.assertEqual((rc, out), (2, None))
        self.assertIn("no_run", err)
        # --follow on a queued job waits until it runs, then prints its output.
        p = subprocess.Popen(
            [sys.executable, TASK, "logs", q, "--host", "s2", "--follow"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1)
        self.assertIsNone(p.poll())
        _ = self.start_daemon("s2")
        out, err = p.communicate(timeout=30)
        self.assertEqual((p.returncode, out.decode()), (0, "hello\n"), err)
        # A job cancelled while queued never ran: --follow ends with no_run.
        c = self.submit(self.make_pkg(), host="s1")
        _ = self.ok("cancel", c, "--host", "s1")
        rc, _, err = self.task("logs", c, "--host", "s1", "--follow")
        self.assertEqual(rc, 2)
        self.assertIn("no_run", err)

    def test_fetch(self) -> None:
        _ = self.start_daemon("s1")
        j = self.submit(
            self.make_pkg(
                script="mkdir -p artifacts; echo a,b > artifacts/x.csv; echo hi; "
                + 'echo \'{"outputs": {"k": 1}}\' > $TASKMGR_RESULT_FILE'
            )
        )
        _ = self.wait_state(j, "succeeded")
        outdir = os.path.join(self.tmp, "results")
        out = self.ok("fetch", j, "--host", "s1", "--output", outdir)
        self.assertFalse(self.get(out, "partial"))
        base = os.path.join(outdir, "s1", j)
        self.assertEqual(self.get(out, "path"), base)
        self.assertNotIn("attempts", self.obj(out))
        for rel in (
            "job.json",
            "runner.json",
            "stdout.log",
            "stderr.log",
            "shim.log",
            "workdir/result.json",
            "workdir/progress.json",
            "workdir/artifacts/x.csv",
            "workdir/task.json",
        ):
            self.assertTrue(os.path.exists(os.path.join(base, rel)), rel)
        self.assertEqual(
            sorted(os.listdir(base)),
            ["job.json", "runner.json", "shim.log", "stderr.log", "stdout.log", "workdir"],
        )  # not package/, package.tar.gz or shim.lock
        with open(os.path.join(base, "stdout.log")) as f:
            self.assertEqual(f.read(), "hi\n")
        self.assertTrue(
            os.path.exists(os.path.join(self.job_dir(j), "workdir", "artifacts", "x.csv"))
        )
        rc, out, _ = self.task("fetch", j, "--host", "s1", "--output", outdir)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "exists"))
        rc, out, _ = self.task("fetch", j, "--host", "s1", "--output", outdir, "--all-attempts")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "bad_args"))
        # A retry is a new job with its own directory.
        new = self.text(self.ok("retry", j, "--host", "s1"), "jobid")
        _ = self.wait_state(new, "succeeded")
        out = self.ok("fetch", new, "--host", "s1", "--output", outdir)
        self.assertEqual(self.get(out, "path"), os.path.join(outdir, "s1", new))
        with open(os.path.join(outdir, "s1", new, "job.json")) as f:
            self.assertIn(j, f.read())  # retry_of

    def test_fetch_of_a_job_that_never_ran(self) -> None:
        outdir = os.path.join(self.tmp, "results")
        q = self.submit(self.make_pkg(), host="s1")  # no daemon: stays queued
        rc, out, _ = self.task("fetch", q, "--host", "s1", "--output", outdir)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "no_run"))
        _ = self.ok("cancel", q, "--host", "s1")
        rc, out, _ = self.task("fetch", q, "--host", "s1", "--output", outdir)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "no_run"))
        self.assertFalse(os.path.exists(os.path.join(outdir, "s1", q)))
        # The remote side refuses on its own, too.
        rc, _, err = self.remote("fetch", q)
        self.assertEqual(rc, 2)
        self.assertIn("no_run", err)

    def test_fetch_running_is_partial(self) -> None:
        _ = self.start_daemon("s1")
        j = self.submit(self.make_pkg(script="echo started; sleep 30"))
        _ = self.wait_for(
            lambda: any(
                os.path.exists(os.path.join(self.root(), d, j, "stdout.log"))
                for d in ("in-progress", "done")
            ),
            10,
            "stdout",
        )
        rc, out, err = self.task(
            "fetch", j, "--host", "s1", "--output", os.path.join(self.tmp, "r")
        )
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.get(out, "partial"))
        self.assertIn("incomplete", err)
