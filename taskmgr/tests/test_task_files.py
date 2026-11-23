"""result.json / progress.json that are not regular files, or not parseable (Section 10.3)."""

import json
import os
import time

from taskmgr.tests.helpers import Env, kill_quiet, pid_alive


class TaskFileTypeTest(Env):
    def workdir(self, jobid: str) -> str:
        return os.path.join(self.job_dir(jobid), "workdir")

    def kept_aside(self, jobid: str) -> list[str]:
        return sorted(n for n in os.listdir(self.workdir(jobid)) if n.startswith("result.json."))

    def assert_invalid_result(self, jobid: str, needle: str) -> None:
        job = self.wait_state(jobid, "invalid_result", timeout=20)
        self.assertEqual(job["state"], "invalid_result")
        errs = self.strs(self.result(jobid), "_taskmgr", "validation_errors")
        self.assertTrue(any(needle in e for e in errs), errs)
        self.assertEqual(self.kept_aside(jobid), ["result.json.corrupt"])
        self.assertEqual(self.runner(jobid).get("final"), True)

    def test_fifo_result_json_ends_invalid_result(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script='mkfifo "$TASKMGR_RESULT_FILE"; exit 0'))
        self.assert_invalid_result(j, "not a regular file")
        # The server still answers.
        b = self.submit(self.make_pkg())
        _ = self.wait_state(b, "succeeded", timeout=20)

    def test_directory_result_json_ends_invalid_result(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script='mkdir -p "$TASKMGR_RESULT_FILE/x"; exit 0'))
        self.assert_invalid_result(j, "not a regular file")

    def test_symlink_result_json_is_not_followed(self) -> None:
        _ = self.start_daemon()
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as f:
            json.dump({"outputs": {"x": 1}}, f)
        j = self.submit(self.make_pkg(script='ln -s %s "$TASKMGR_RESULT_FILE"; exit 0' % target))
        self.assert_invalid_result(j, "not a regular file")
        with open(target) as f:
            self.assertEqual(json.load(f), {"outputs": {"x": 1}})  # untouched

    def test_deeply_nested_result_json_ends_invalid_result(self) -> None:
        _ = self.start_daemon()
        script = (
            "python3 -c 'import sys; sys.stdout.write(\"[\" * 200000)'"
            + ' > "$TASKMGR_RESULT_FILE"; exit 0'
        )
        j = self.submit(self.make_pkg(script=script))
        self.assert_invalid_result(j, "is corrupt")

    def test_fifo_result_json_found_by_the_daemon_does_not_stall_the_server(self) -> None:
        # The shim dies; the task leaves a FIFO as result.json and exits. The daemon's
        # check finalizes the job and keeps serving requests.
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "go")
        script = 'mkfifo "$TASKMGR_RESULT_FILE"; while [ ! -f %s ]; do sleep 0.05; done' % gate
        j = self.submit(self.make_pkg(script=script))
        r = self.wait_runner(j, "pgid")
        fifo = os.path.join(self.workdir(j), "result.json")
        _ = self.wait_for(lambda: os.path.lexists(fifo), 10, "fifo")
        kill_quiet(self.num(r, "shim_pid"))
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "shim_pid")), 10, "shim death")
        with open(gate, "w"):
            pass
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "task end")
        job = self.wait_state(j, "failed", timeout=20)
        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")
        self.assertEqual(self.kept_aside(j), ["result.json.corrupt"])
        rc, out, _ = self.task("create", "job", self.make_pkg(), "--host", "s1", timeout=20)
        self.assertEqual(rc, 0, out)
        _ = self.wait_state(self.text(out, "jobid"), "succeeded", timeout=20)

    def _status_with_progress(self, make: str) -> object:
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "go")
        script = '%s "$TASKMGR_PROGRESS_FILE"; while [ ! -f %s ]; do sleep 0.05; done' % (
            make,
            gate,
        )
        j = self.submit(self.make_pkg(script=script))
        _ = self.wait_runner(j, "pgid")
        prog = os.path.join(self.workdir(j), "progress.json")
        _ = self.wait_for(
            lambda: os.path.lexists(prog) and not os.path.isfile(prog), 10, "progress.json"
        )
        t0 = time.monotonic()
        rc, out, err = self.task("get", "job", j, timeout=20)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(rc, 0, err)
        with open(gate, "w"):
            pass
        return self.get(out, "progress", "error")

    def test_fifo_progress_json_does_not_block_status(self) -> None:
        err = self._status_with_progress('rm -f "$TASKMGR_PROGRESS_FILE"; mkfifo')
        self.assertIn("not a regular file", str(err))

    def test_directory_progress_json_reports_an_error(self) -> None:
        err = self._status_with_progress('rm -f "$TASKMGR_PROGRESS_FILE"; mkdir')
        self.assertIn("not a regular file", str(err))
