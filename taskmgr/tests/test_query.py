"""Job discovery, status lookup across servers, --wait, logs and fetch (Sections 14, 15, 16)."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import cast
from unittest import mock

from taskmgr.common import DEFAULT_REMOTE_CONFIG, TaskmgrError, override
from taskmgr.records import JsonDict, JsonValue, Server
from taskmgr.store import Store, end_without_result, finalize_run
from taskmgr.tests.helpers import FAST_CONFIG, TASK, Env


class QueryTest(Env):
    servers: tuple[str, ...] = ("s1", "s2", "s3")

    def unreachable(self, name: str, on: bool = True) -> None:
        if on:
            self.server(name)["local"] = {"fail": "ssh: connect to host %s timed out" % name}
        else:
            _ = self.server(name).pop("local", None)
        self.write_machines()

    def test_a_server_that_does_not_answer_times_out(self) -> None:
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")
        # connects, never answers (the remote arguments land in $1.. and are ignored)
        self.server("s2")["remote_command"] = "sh -c 'sleep 60' sh"
        self.write_machines()
        env = {"TASKMGR_QUERY_TIMEOUT": "1"}
        start = time.monotonic()
        rc, out, err = self.task("jobs", text=True, env=env)
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(rc, 0)
        self.assertIn(a, self.text(out))
        self.assertIn("warning: s2 unreachable, its jobs are not listed: no answer within 1s", err)
        # The jobid lookup gives up on s2 the same way.
        st = self.ok("status", a, env=env)
        self.assertEqual((self.get(st, "host"), self.get(st, "complete")), ("s1", False))

    def test_ssh_connect_timeout(self) -> None:
        from taskmgr.transport import Remote

        entry: Server = {
            "name": "gpu1",
            "transport": "ssh",
            "ssh": {"host": "gpu1"},
            "remote_root": "/r",
            "remote_command": "taskmgr-remote",
        }
        argv = Remote(entry).ssh_argv()
        self.assertIn("ConnectTimeout=10", argv)
        entry["ssh"] = {"host": "gpu1", "options": ["-o", "ConnectTimeout=3"]}
        argv = Remote(entry).ssh_argv()
        self.assertEqual([a for a in argv if "ConnectTimeout" in a], ["ConnectTimeout=3"])

    def test_disabled_servers_are_skipped(self) -> None:
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")
        c = self.submit(self.make_pkg(task_id="CC-03"), host="s3")
        self.unreachable("s3")  # down, and disabled: no query waits for it
        _ = self.ok("host", "disable", "s3")
        self.server("s3")["enabled"] = False  # the fixture rewrites machines.json from this
        out = self.ok("jobs")
        self.assertEqual([j["jobid"] for j in self.objs(out, "jobs")], [a])
        self.assertEqual(
            (self.get(out, "skipped"), self.get(out, "errors"), self.get(out, "complete")),
            (["s3"], [], True),
        )
        rc, text, err = self.task("jobs", text=True)
        self.assertEqual(rc, 0)
        self.assertIn(a, self.text(text))
        self.assertEqual(err, "skipped: disabled, not queried: s3\n")
        # The jobid lookup skips it too, and says so when the job is not found.
        rc, out, err = self.task("status", c)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "not_found"))
        self.assertIn("disabled, not searched: s3", self.text(out, "error", "message"))
        # --host still reaches a disabled server.
        self.unreachable("s3", on=False)
        self.assertEqual(self.get(self.ok("status", c, "--host", "s3"), "state"), "queued")
        self.assertEqual(len(self.objs(self.ok("jobs", "--host", "s3"), "jobs")), 1)
        # Every server disabled: an error naming them, unless --host.
        for name in ("s1", "s2"):
            _ = self.ok("host", "disable", name)
        rc, out, _ = self.task("jobs")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "no_enabled_hosts"))
        _ = self.ok("host", "enable", "s3")
        out = self.ok("jobs")
        self.assertEqual(
            ([j["jobid"] for j in self.objs(out, "jobs")], self.get(out, "skipped")),
            ([c], ["s1", "s2"]),
        )

    def test_jobs_across_servers(self) -> None:
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")
        b = self.submit(self.make_pkg(task_id="BB-02"), host="s3")
        out = self.ok("jobs")
        self.assertTrue(self.get(out, "complete"))
        self.assertEqual(self.get(out, "errors"), [])
        got = {(j["host"], j["jobid"], j["task_id"], j["state"]) for j in self.objs(out, "jobs")}
        self.assertEqual(got, {("s1", a, "AA-01", "queued"), ("s3", b, "BB-02", "queued")})
        for j in self.objs(out, "jobs"):
            self.assertIn("submitted_at", j)
            self.assertEqual((j["retry_of"], j["retried_as"]), (None, []))
            self.assertNotIn("attempt", j)
            self.assertNotIn("attempt_count", j)
        out = self.ok("jobs", "--host", "s3")
        self.assertEqual([j["jobid"] for j in self.objs(out, "jobs")], [b])
        _ = self.ok("cancel", a, "--host", "s1")
        out = self.ok("jobs", "--state", "cancelled")
        self.assertEqual([j["jobid"] for j in self.objs(out, "jobs")], [a])
        self.unreachable("s2")
        out = self.ok("jobs")
        self.assertFalse(self.get(out, "complete"))
        self.assertEqual([e["host"] for e in self.objs(out, "errors")], ["s2"])
        self.assertIn("timed out", self.text(out, "errors", 0, "error"))
        self.assertEqual(len(self.arr(out, "jobs")), 2)
        self.unreachable("s1")
        self.unreachable("s3")
        rc, out, _ = self.task("jobs")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "all_hosts_failed")

    def test_jobs_limit(self) -> None:
        oldest = self.submit(self.make_pkg(task_id="AA-01"), host="s1")
        time.sleep(1.1)  # submitted_at has 1 s resolution; make the order unambiguous
        pkg = self.make_pkg(task_id="BB-02")
        newer = [self.submit(pkg, host="s1" if i % 2 else "s3") for i in range(10)]

        def ids(*args: str) -> list[object]:
            return [j["jobid"] for j in self.objs(self.ok("jobs", *args), "jobs")]

        # The limit counts jobs over all servers, not per server.
        self.assertEqual(sorted(map(str, ids())), sorted(newer))
        self.assertEqual(ids("-n", "11")[0], oldest)
        self.assertEqual(len(ids("-n", "100")), 11)
        self.assertEqual(len(ids("-n", "3")), 3)
        self.assertEqual(len(ids("--host", "s1", "-n", "6")), 6)
        rc, out, _ = self.task("jobs", "-n", "2", text=True)
        assert isinstance(out, str)
        self.assertEqual((rc, len(out.splitlines())), (0, 3))
        # --state filters first, so an old job in that state is still found.
        _ = self.ok("cancel", oldest, "--host", "s1")
        self.assertEqual(ids("--state", "cancelled", "-n", "1"), [oldest])
        for bad in ("0", "-1", "x"):
            rc, _, err = self.task("jobs", "-n", bad, text=True)
            self.assertEqual(rc, 2)
            self.assertIn("positive integer", err)

    def test_jobs_table(self) -> None:
        a = self.submit(self.make_pkg(task_id="AA-01"), host="s1")  # s1 has no daemon: stays queued
        time.sleep(1.1)  # submitted_at has 1 s resolution; make the order unambiguous
        b = self.submit(self.make_pkg(task_id="BB-02"), host="s3")
        _ = self.start_daemon("s3")
        _ = self.wait_state(b, "succeeded", name="s3")
        rc, out, err = self.task("jobs", text=True)
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
        # Color: on by default (on a pipe too), off with --plain or NO_COLOR, and the
        # escape codes do not change the layout.
        self.assertNotIn("\x1b[", out)
        rc, colored, _ = self.task("jobs", text=True, color=True)
        assert isinstance(colored, str)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[1mJOBID", colored)  # bold header
        self.assertIn("\x1b[33mqueued\x1b[0m", colored)  # yellow state
        self.assertIn("\x1b[32msucceeded\x1b[0m", colored)  # green state
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", colored), out)
        rc, out2, _ = self.task("jobs", text=True, color=True, env={"NO_COLOR": "1"})
        self.assertEqual((rc, out2), (0, out))
        rc, out, _ = self.task("jobs", "--state", "running", text=True)
        self.assertEqual((rc, out), (0, "JOBID  TASK_ID  SUBMITTED  HOST  STATE\n"))
        # Unreachable servers: still a table on stdout, a warning on stderr, exit 0.
        self.unreachable("s2")
        rc, out, err = self.task("jobs", text=True)
        assert isinstance(out, str)
        self.assertEqual((rc, len(out.splitlines())), (0, 3))
        self.assertIn("warning: s2 unreachable", err)
        # Nothing reachable: exit 2, nothing on stdout.
        self.unreachable("s1")
        self.unreachable("s3")
        rc, out, err = self.task("jobs", text=True)
        self.assertEqual((rc, out), (2, None))
        self.assertIn("no server could be queried", err)
        rc, out, err = self.task("jobs", "--host", "nope", text=True)
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
        # Count every remote call per server: the wrapper appends a line, then runs the
        # real taskmgr-remote.
        counts = {n: os.path.join(self.tmp, "calls-" + n) for n in self.servers}
        for n in self.servers:
            self.server(n)["remote_command"] = 'sh -c \'echo x >> "$0"; exec "$@"\' %s %s' % (
                counts[n],
                self.server(n)["remote_command"],
            )
        self.write_machines()

        def calls(n: str) -> int:
            try:
                with open(counts[n]) as f:
                    return len(f.readlines())
            except FileNotFoundError:
                return 0

        rc, out, err = self.task("status", j, "--wait", "--poll-interval", "0.2")
        self.assertEqual(
            (rc, self.get(out, "_taskmgr", "status"), self.get(out, "host")),
            (1, "failed", "s2"),
            err,
        )
        # One cross-server lookup, then only s2 (where the job is) is polled.
        self.assertEqual((calls("s1"), calls("s3")), (1, 1))
        self.assertGreater(calls("s2"), 2)
        self.assertNotIn("unreachable", err)

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

    def test_ssh_keepalive_options(self) -> None:
        from taskmgr.transport import Remote

        entry: Server = {
            "name": "gpu1",
            "transport": "ssh",
            "remote_root": "/r",
            "remote_command": "taskmgr-remote",
        }
        argv = Remote(entry).ssh_argv()
        self.assertIn("ServerAliveInterval=15", argv)
        self.assertIn("ServerAliveCountMax=3", argv)
        entry["ssh"] = {"options": ["-o", "ServerAliveInterval=5"]}
        argv = Remote(entry).ssh_argv()
        self.assertEqual(
            [a for a in argv if "ServerAlive" in a],
            ["ServerAliveInterval=5", "ServerAliveCountMax=3"],
        )

    def test_single_server_calls_time_out_by_default(self) -> None:
        from taskmgr.transport import ConnError, Remote

        self.server("s2")["remote_command"] = "sh -c 'sleep 60' sh"
        start = time.monotonic()
        with (
            mock.patch.dict(os.environ, {"TASKMGR_QUERY_TIMEOUT": "1"}),
            self.assertRaises(ConnError) as cm,
        ):
            _ = Remote(self.server("s2")).call(["status", "0123456789abcdef"])
        self.assertLess(time.monotonic() - start, 15)
        self.assertIn("no answer within 1s", cm.exception.message)

    def test_remote_without_reply_or_with_a_malformed_error(self) -> None:
        from taskmgr.transport import ConnError, Remote, query_all

        self.server("s2")["remote_command"] = "sh -c 'echo boom >&2; exit 3' sh"
        with self.assertRaises(ConnError) as cm:  # outcome unknown, like a lost connection
            _ = Remote(self.server("s2")).call(["jobs"])
        self.assertEqual(cm.exception.code, "remote_error")
        self.assertIn("boom", cm.exception.message)
        self.server("s3")["remote_command"] = (
            'sh -c \'echo "{\\"error\\": 5, \\"protocol_version\\": 2}"\' sh'
        )
        results, errors = query_all(self.machines, ["jobs"])
        self.assertEqual(list(results), ["s1"])
        self.assertEqual(
            [(e["host"], e["code"]) for e in errors],
            [("s2", "remote_error"), ("s3", "remote_error")],
        )
        self.assertIn("malformed", errors[1]["error"])

    def test_ssh_failure_detection_for_scripts(self) -> None:
        from taskmgr.transport import looks_like_ssh_failure

        self.assertTrue(
            looks_like_ssh_failure("ssh: connect to host x port 22: Connection refused")
        )
        self.assertTrue(looks_like_ssh_failure("Permission denied (publickey).\n"))
        self.assertFalse(looks_like_ssh_failure(""))
        self.assertFalse(looks_like_ssh_failure("my script failed\n"))

    def test_bad_machine_lists(self) -> None:
        from taskmgr.transport import load_machines

        home = os.path.join(self.tmp, "h2")
        os.makedirs(os.path.join(home, "machines.json"))  # unreadable: a directory
        with self.assertRaises(TaskmgrError) as cm:
            _ = load_machines(home)
        self.assertEqual(cm.exception.code, "bad_machines")
        os.rmdir(os.path.join(home, "machines.json"))
        names: list[JsonValue] = [["a"], 5]
        for name in names:
            entry: JsonDict = {"name": name, "remote_root": "/r", "remote_command": "x"}
            with open(os.path.join(home, "machines.json"), "w") as f:
                json.dump({"servers": [entry]}, f)
            with self.assertRaises(TaskmgrError) as cm:
                _ = load_machines(home)
            self.assertEqual(cm.exception.code, "bad_machines")


class StoreTest(Env):
    """Store-level behaviour, exercised in-process on a server root."""

    def store(self) -> Store:
        return Store(self.root())

    def test_a_job_moving_back_to_queue_is_not_missed(self) -> None:
        a = self.submit(self.make_pkg())
        b = self.submit(self.make_pkg())
        in_progress = os.path.join(self.root(), "in-progress")
        os.rename(self.job_dir(b), os.path.join(in_progress, b))
        st = self.store()
        it = st.iter_jobs()
        first = next(it)  # queue/ has been listed
        # b goes back to queue/ (a "GPU busy" re-queue) before in-progress/ is listed.
        os.rename(os.path.join(in_progress, b), os.path.join(self.root(), "queue", b))
        seen = {first[1]["jobid"]} | {j["jobid"] for _, j in it}
        self.assertEqual(seen, {a, b})
        self.assertEqual({j["jobid"] for _, j in st.iter_jobs(active=True)}, {a, b})

    def test_lookup_confirms_a_miss_under_the_state_lock(self) -> None:
        a = self.submit(self.make_pkg())

        class Racy(Store):
            misses: int = 1
            locked_scans: int = 0

            @override
            def _scan_job_dir(self, jobid: str) -> str | None:
                if self._lock_depth:
                    self.locked_scans += 1
                if self.misses:  # the directory was between two places
                    self.misses -= 1
                    return None
                return super()._scan_job_dir(jobid)

        st = Racy(self.root())
        jd, job = st.load_job(a)
        self.assertEqual((jd, job["jobid"], st.locked_scans), (self.job_dir(a), a, 1))
        self.assertIsNone(st.find_job_dir("0123456789abcdef"))

    def test_corrupt_counter_continues_after_the_highest_seq(self) -> None:
        a = self.submit(self.make_pkg())
        b = self.submit(self.make_pkg())
        with open(os.path.join(self.root(), "locks", "counters.json"), "w") as f:
            _ = f.write("{not json")
        c = self.submit(self.make_pkg())
        self.assertEqual([self.job(j)["enqueue_seq"] for j in (a, b, c)], [1, 2, 3])
        os.unlink(os.path.join(self.root(), "locks", "counters.json"))
        d = self.submit(self.make_pkg())
        self.assertEqual(self.job(d)["enqueue_seq"], 4)

    def test_config_types_are_checked(self) -> None:
        self.set_config(gpu_busy_exit_code=None, stop_grace_seconds=0.5, max_cpu_jobs=2)
        self.assertEqual(self.store().config["max_cpu_jobs"], 2)
        defaults = cast(JsonDict, cast(object, DEFAULT_REMOTE_CONFIG))
        bad: list[JsonDict] = [
            {"max_cpu_jobs": []},
            {"max_auto_restarts": True},
            {"stop_grace_seconds": "1"},
            {"base_env": {"A": 1}},
            {"max_auto_restarts": None},
        ]
        for kw in bad:
            self.set_config("s1", **kw)
            with self.assertRaises(TaskmgrError) as cm:
                _ = self.store().config
            self.assertEqual(cm.exception.code, "bad_config", kw)
            self.set_config("s1", **{k: FAST_CONFIG.get(k, defaults[k]) for k in kw})
        for text in ("[]", "0"):
            with open(os.path.join(self.root(), "config.json"), "w") as f:
                _ = f.write(text)
            with self.assertRaises(TaskmgrError) as cm:
                _ = self.store().config
            self.assertEqual(cm.exception.code, "bad_config", text)

    def _as_dispatched(self, jobid: str, state: str, where: str) -> str:
        """Rewrite a queued job as dispatched and in `state`, with a non-final run record."""
        jd = self.job_dir(jobid)
        job: dict[str, object] = dict(self.job(jobid))
        job.update({"state": state, "dispatched_at": "2026-10-29T08:00:00Z"})
        with open(os.path.join(jd, "job.json"), "w") as f:
            json.dump(job, f)
        runner = {"jobid": jobid, "final": False, "exited": True, "return_code": 0}
        with open(os.path.join(jd, "runner.json"), "w") as f:
            json.dump(runner, f)
        new = os.path.join(self.root(), where, jobid)
        if new != jd:
            os.rename(jd, new)
        return new

    def test_finalize_writes_nothing_for_a_queued_or_terminal_job(self) -> None:
        for state, where in (("queued", "queue"), ("cancelled", "done")):
            j = self.submit(self.make_pkg())
            jd = self._as_dispatched(j, state, where)
            job = finalize_run(self.store(), j)
            self.assertEqual(job["state"], state)
            self.assertFalse(self.runner(j).get("final"), state)
            self.assertFalse(os.path.exists(os.path.join(jd, "workdir", "result.json")), state)

    def test_end_without_result_recognizes_its_own_block(self) -> None:
        j = self.submit(self.make_pkg())
        jd = self._as_dispatched(j, "running", "in-progress")
        os.makedirs(os.path.join(jd, "workdir"))
        earlier = {"outputs": {}, "_taskmgr": {"final": True, "jobid": j, "status": "failed"}}
        with open(os.path.join(jd, "workdir", "result.json"), "w") as f:
            json.dump(earlier, f)
        st = self.store()
        with st.state_lock():
            jd, job = st.load_job(j)
            runner, _ = st.read_runner(jd)
            assert runner is not None
            _ = end_without_result(st, jd, job, "failed", None, runner)
        self.assertEqual(self.job(j)["state"], "failed")
        self.assertEqual(self.get(self.result(j), "_taskmgr", "warnings"), [])
