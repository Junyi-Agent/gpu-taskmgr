"""`task wait job` without --host: the jobid lookup retries unreachable servers."""

import os
import time

from taskmgr.tests.helpers import OK_RESULT, Env


class WaitLookupTest(Env):
    servers: tuple[str, ...] = ("s1", "s2")

    def test_lookup_retries_connection_failures(self) -> None:
        """While the server holding the job is down, the lookup on every server cannot find
        it; with --wait that is retried within --retry-timeout, as with --host."""
        _ = self.start_daemon("s1")
        flag = os.path.join(self.tmp, "down")
        self.server("s1")["local"] = {"fail_if_exists": flag}
        self.write_machines()
        j = self.submit(self.make_pkg(script="sleep 1; " + OK_RESULT))
        open(flag, "w").close()
        # The server comes back once the CLI has reported a failed query.
        rc, out, err = self.task_when(
            "retrying",
            lambda: os.unlink(flag),
            "wait",
            "job",
            j,
            "--poll-interval",
            "0.2",
            "--retry-timeout",
            "30",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("retrying", err)
        self.assertEqual(self.get(out, "_taskmgr", "status"), "succeeded")
        # A finished job has jobid and state at the top level, like a live one.
        self.assertEqual((self.get(out, "jobid"), self.get(out, "state")), (j, "succeeded"))
        st = self.ok("get", "job", j)
        self.assertEqual((self.get(st, "jobid"), self.get(st, "state")), (j, "succeeded"))
        self.assertEqual(self.get(st, "_taskmgr", "jobid"), j)
        # Beyond --retry-timeout it gives up with a connection error.
        open(flag, "w").close()
        rc, out, _ = self.task("wait", "job", j, "--poll-interval", "0.2", "--retry-timeout", "1")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "connection_error"))
        # Without --wait the lookup still fails at once.
        rc, out, _ = self.task("get", "job", j)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "incomplete"))
        os.unlink(flag)


class WaitDeadlineTest(Env):
    """Review P1-6: --timeout is the total wait, also while a single query hangs."""

    servers: tuple[str, ...] = ("s1", "s2")

    def test_timeout_bounds_a_hanging_query(self) -> None:
        j = self.submit(self.make_pkg(script="sleep 30"), host="s1")
        # s2 accepts the connection and never answers (the default query timeout is 30 s).
        self.server("s2")["remote_command"] = "sh -c 'exec sleep 60' sh"
        self.write_machines()
        for host in (["--host", "s2"], []):  # one server, and the lookup on every server
            t0 = time.monotonic()
            rc, out, err = self.task(
                "wait", "job", j, *host, "--poll-interval", "0.2", "--timeout", "1"
            )
            elapsed = time.monotonic() - t0
            self.assertEqual((rc, self.get(out, "error", "code")), (2, "wait_timeout"), err)
            self.assertLess(elapsed, 8, host)
