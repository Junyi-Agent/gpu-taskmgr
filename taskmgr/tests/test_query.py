"""Job discovery, status lookup across servers, --wait, logs and fetch (Sections 14, 15, 16)."""

import os
import shutil
import subprocess
import sys
import threading
import time

from helpers import TASK, Env


class QueryTest(Env):
    servers = ("s1", "s2", "s3")

    def setUp(self):
        super().setUp()
        self.machines[2]["enabled"] = False  # disabled servers are still queried
        self.write_machines()

    def unreachable(self, name, on=True):
        if on:
            self.server(name)["local"] = {"fail": "ssh: connect to host %s timed out" % name}
        else:
            self.server(name).pop("local", None)
        self.write_machines()

    def test_jobs_across_servers(self):
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")
        b = self.submit(self.make_pkg(task_id="BB-02"), host="s3")
        out = self.ok("jobs")
        self.assertTrue(out["complete"])
        self.assertEqual(out["errors"], [])
        got = {(j["host"], j["jobid"], j["task_id"], j["state"]) for j in out["jobs"]}
        self.assertEqual(got, {("s1", a, "AA-01", "queued"), ("s3", b, "BB-02", "queued")})
        for j in out["jobs"]:
            self.assertIn("submitted_at", j)
            self.assertIn("attempt", j)
        out = self.ok("jobs", "--host", "s3")
        self.assertEqual([j["jobid"] for j in out["jobs"]], [b])
        self.ok("cancel", a, "--host", "s1")
        out = self.ok("jobs", "--state", "cancelled")
        self.assertEqual([j["jobid"] for j in out["jobs"]], [a])
        self.unreachable("s2")
        out = self.ok("jobs")
        self.assertFalse(out["complete"])
        self.assertEqual([e["host"] for e in out["errors"]], ["s2"])
        self.assertIn("timed out", out["errors"][0]["error"])
        self.assertEqual(len(out["jobs"]), 2)
        self.unreachable("s1")
        self.unreachable("s3")
        rc, out, _ = self.task("jobs")
        self.assertEqual(rc, 2)

    def test_status_lookup_table(self):
        j = self.submit(self.make_pkg(), host="s3")
        out = self.ok("status", j)
        self.assertEqual((out["host"], out["state"], out["complete"]), ("s3", "queued", True))
        self.unreachable("s1")
        rc, out, err = self.task("status", j)
        self.assertEqual(rc, 0)
        self.assertFalse(out["complete"])
        self.assertIn("uniqueness", err)
        rc, out, _ = self.task("status", "0123456789abcdef")
        self.assertEqual((rc, out["error"]["code"]), (2, "incomplete"))
        self.unreachable("s1", False)
        rc, out, _ = self.task("status", "0123456789abcdef")
        self.assertEqual((rc, out["error"]["code"]), (2, "not_found"))
        # Same jobid on two servers: ambiguity.
        shutil.copytree(self.job_dir(j, "s3"), os.path.join(self.root("s2"), "queue", j),
                        symlinks=True)
        rc, out, _ = self.task("status", j)
        self.assertEqual((rc, out["error"]["code"]), (2, "ambiguous"))
        self.assertEqual(sorted(out["error"]["details"]["hosts"]), ["s2", "s3"])
        out = self.ok("status", j, "--host", "s2")
        self.assertEqual(out["host"], "s2")
        rc, out, _ = self.task("status", "0123456789abcdef", "--host", "s1")
        self.assertEqual((rc, out["error"]["code"]), (2, "not_found"))

    def test_wait_outcomes(self):
        self.start_daemon("s1")
        ok = self.submit(self.make_pkg(
            script="sleep 1; echo '{\"outputs\": {}}' > $TASKMGR_RESULT_FILE"))
        rc, out, _ = self.task("status", ok, "--wait", "--poll-interval", "0.2")
        self.assertEqual(rc, 0)
        self.assertEqual(out["_taskmgr"]["status"], "succeeded")
        bad = self.submit(self.make_pkg(script="exit 2"))
        rc, out, _ = self.task("status", bad, "--host", "s1", "--wait", "--poll-interval", "0.2")
        self.assertEqual(rc, 1)
        self.assertEqual(out["_taskmgr"]["status"], "failed")
        slow = self.submit(self.make_pkg(script="sleep 30"))
        t0 = time.time()
        rc, out, _ = self.task("status", slow, "--host", "s1", "--wait", "--poll-interval", "0.2",
                               "--timeout", "1")
        self.assertEqual(rc, 2)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(out["state"], "running")
        self.assertEqual(out["error"]["code"], "wait_timeout")
        self.assertEqual(self.job(slow)["state"], "running")  # wait never changes remote state

    def test_wait_retries_connection_failures(self):
        self.start_daemon("s1")
        flag = os.path.join(self.tmp, "down")
        self.server("s1")["local"] = {"fail_if_exists": flag}
        self.write_machines()
        j = self.submit(self.make_pkg(
            script="sleep 2; echo '{\"outputs\": {}}' > $TASKMGR_RESULT_FILE"))
        open(flag, "w").close()
        threading.Timer(1.5, lambda: os.unlink(flag)).start()
        rc, out, err = self.task("status", j, "--host", "s1", "--wait", "--poll-interval", "0.2",
                                 "--retry-timeout", "30")
        self.assertEqual(rc, 0, err)
        self.assertIn("retrying", err)
        # Beyond --retry-timeout it gives up with a connection error.
        j2 = self.submit(self.make_pkg(script="sleep 30"))
        open(flag, "w").close()
        rc, out, _ = self.task("status", j2, "--host", "s1", "--wait", "--poll-interval", "0.2",
                               "--retry-timeout", "1")
        self.assertEqual((rc, out["error"]["code"]), (2, "connection_error"))
        # --timeout still applies while connection failures are being retried.
        t0 = time.time()
        rc, out, _ = self.task("status", j2, "--host", "s1", "--wait", "--poll-interval", "0.2",
                               "--retry-timeout", "60", "--timeout", "1")
        self.assertEqual((rc, out["error"]["code"]), (2, "wait_timeout"))
        self.assertLess(time.time() - t0, 15)
        os.unlink(flag)
        self.assertEqual(self.job(j2)["state"], "running")

    def test_wait_without_host_locates_once(self):
        self.start_daemon("s2")
        j = self.submit(self.make_pkg(script="sleep 1.5; exit 3"), host="s2")
        flag = os.path.join(self.tmp, "s1down")
        self.server("s1")["local"] = {"fail_if_exists": flag}
        self.write_machines()
        p = subprocess.Popen([sys.executable, TASK, "status", j, "--wait", "--poll-interval",
                              "0.2"], env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        time.sleep(0.8)
        open(flag, "w").close()  # s1 going down after the lookup does not matter
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1, err)

    def test_logs_and_follow(self):
        self.start_daemon("s1")
        j = self.submit(self.make_pkg(
            script="for i in 1 2 3; do echo line$i; sleep 0.5; done; echo err >&2"))
        p = subprocess.Popen([sys.executable, TASK, "logs", j, "--host", "s1", "--follow"],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(out.decode(), "line1\nline2\nline3\n")
        self.wait_state(j, "invalid_result")
        rc, out, _ = self.task("logs", j, "--host", "s1", "--stream", "stderr")
        self.assertEqual((rc, out), (0, "err\n"))
        rc, out, _ = self.task("logs", j, "--host", "s1", "--stream", "shim")
        self.assertIn("finalized", out)
        rc, out, _ = self.task("logs", j, "--host", "s1", "--attempt", "9")
        self.assertEqual(rc, 2)

    def test_fetch(self):
        self.start_daemon("s1")
        j = self.submit(self.make_pkg(
            script="mkdir -p artifacts; echo a,b > artifacts/x.csv; echo hi; "
                   "echo '{\"outputs\": {\"k\": 1}}' > $TASKMGR_RESULT_FILE"))
        self.wait_state(j, "succeeded")
        outdir = os.path.join(self.tmp, "results")
        out = self.ok("fetch", j, "--host", "s1", "--output", outdir)
        self.assertFalse(out["partial"])
        base = os.path.join(outdir, "s1", j, "attempts", "0001")
        for rel in ("runner.json", "stdout.log", "stderr.log", "shim.log",
                    "workdir/result.json", "workdir/progress.json", "workdir/artifacts/x.csv",
                    "workdir/task.json"):
            self.assertTrue(os.path.exists(os.path.join(base, rel)), rel)
        with open(os.path.join(base, "stdout.log")) as f:
            self.assertEqual(f.read(), "hi\n")
        self.assertTrue(os.path.exists(os.path.join(self.job_dir(j), "attempts", "0001",
                                                    "workdir", "artifacts", "x.csv")))
        rc, out, _ = self.task("fetch", j, "--host", "s1", "--output", outdir)
        self.assertEqual((rc, out["error"]["code"]), (2, "exists"))
        # --all-attempts after a retry.
        self.ok("retry", j, "--host", "s1")
        self.wait_for(lambda: len(self.job(j)["attempts"]) == 2 and
                      self.job(j)["state"] == "succeeded", 20, "retry")
        out2 = os.path.join(self.tmp, "results2")
        out = self.ok("fetch", j, "--host", "s1", "--output", out2, "--all-attempts")
        self.assertEqual([a["attempt"] for a in out["attempts"]], [1, 2])
        rc, out, _ = self.task("fetch", j, "--host", "s1", "--output", out2, "--attempt", "2")
        self.assertEqual(rc, 2)

    def test_fetch_running_is_partial(self):
        self.start_daemon("s1")
        j = self.submit(self.make_pkg(script="echo started; sleep 30"))
        self.wait_for(lambda: any(os.path.exists(os.path.join(self.root(), d, j, "attempts",
                                                              "0001", "stdout.log"))
                                  for d in ("in-progress", "done")), 10, "stdout")
        rc, out, err = self.task("fetch", j, "--host", "s1", "--output",
                                 os.path.join(self.tmp, "r"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["partial"])
        self.assertIn("incomplete", err)
