"""Execution outcomes, environment, result merge, pre-start checks, stop procedure, retry."""

import json
import os
import time
from typing import cast

from taskmgr.records import JsonValue, override
from taskmgr.tests.helpers import GPUS, OK_RESULT, Env, pid_alive

PY_RESULT = (
    'python3 -c \'import json,os;json.dump(%s, open(os.environ["TASKMGR_RESULT_FILE"], "w"))\''
)


def write_result(obj: object) -> str:
    return PY_RESULT % json.dumps(obj).replace("'", "")


class ExecTest(Env):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.start_daemon(env={"DAEMON_ONLY_VAR": "leak"})

    def test_success_and_environment(self) -> None:
        venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(venv, "bin"))
        self.set_config(base_env={"LANG": "en_US.UTF-8", "EXTRA_BASE": "1"})
        out = os.path.join(self.tmp, "env.json")
        script = (
            'python3 -c \'import json,os,sys; json.dump(dict(os.environ), open("%s", "w"));'
            + 'sys.stdout.write("hello\\n"); sys.stderr.write("oops\\n")\';'
        ) % out + write_result({"outputs": {"speedup": 1.5}, "notes": "kept"})
        pkg = self.make_pkg(
            script=script,
            gpus=[1],
            required=["speedup"],
            env={"NCCL_DEBUG": "WARN", "LANG": "C"},
            extra={"environment": {"venv": venv, "required_paths": [self.tmp]}},
        )
        j = self.submit(pkg)
        job = self.wait_state(j, "succeeded")
        res = self.result(j)
        self.assertEqual(res["outputs"], {"speedup": 1.5})
        self.assertEqual(res["notes"], "kept")
        m = self.obj(res, "_taskmgr")
        self.assertEqual(
            (m["status"], m["return_code"], m["signal"], m["final"]), ("succeeded", 0, None, True)
        )
        for k in (
            "jobid",
            "task_id",
            "retry_of",
            "host",
            "gpus",
            "started_at",
            "ended_at",
            "validation_errors",
            "error",
            "warnings",
            "paths",
        ):
            self.assertIn(k, m)
        self.assertNotIn("attempt", m)
        self.assertIsNone(m["retry_of"])
        self.assertEqual(m["gpus"], [{"declared": 1, "uuid": GPUS[1][1]}])
        with open(out) as f:
            env = self.obj(cast(JsonValue, json.load(f)))
        self.assertNotIn("DAEMON_ONLY_VAR", env)
        self.assertEqual(env["PATH"], venv + "/bin:/usr/local/bin:/usr/bin:/bin")
        self.assertEqual(env["VIRTUAL_ENV"], venv)
        self.assertEqual(env["LANG"], "C")  # task env overrides base_env
        self.assertEqual(env["EXTRA_BASE"], "1")
        self.assertEqual(env["NCCL_DEBUG"], "WARN")
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], GPUS[1][1])
        self.assertEqual(env["CUDA_DEVICE_ORDER"], "PCI_BUS_ID")
        self.assertEqual(env["TASKMGR_JOBID"], j)
        self.assertEqual(env["TASKMGR_TASK_ID"], "AA-01")
        self.assertNotIn("TASKMGR_ATTEMPT", env)
        # While running, the job lives in in-progress/; afterwards in done/.
        wd = os.path.join(self.root(), "in-progress", j, "workdir")
        self.assertEqual(env["TASKMGR_WORKDIR"], wd)
        final_wd = os.path.join(self.root(), "done", j, "workdir")
        self.assertEqual(self.get(m, "paths", "workdir"), final_wd)
        self.assertEqual(self.get(m, "paths", "job_dir"), os.path.join(self.root(), "done", j))
        self.assertFalse(os.path.exists(os.path.join(self.job_dir(j), "attempts")))
        self.assertEqual(self.get(m, "paths", "artifacts"), os.path.join(final_wd, "artifacts"))
        self.assertEqual(env["TASKMGR_PROGRESS_FILE"], os.path.join(wd, "progress.json"))
        self.assertEqual(env["TASKMGR_RESULT_FILE"], os.path.join(wd, "result.json"))
        self.assertIn("HOME", env)
        self.assertIn("USER", env)
        adir = os.path.dirname(final_wd)
        with open(os.path.join(adir, "stdout.log")) as f:
            self.assertEqual(f.read(), "hello\n")
        with open(os.path.join(adir, "stderr.log")) as f:
            self.assertEqual(f.read(), "oops\n")
        runner = self.runner(j)
        for k in (
            "boot_id",
            "shim_pid",
            "shim_start_time",
            "pgid",
            "leader_pid",
            "leader_start_time",
            "argv_hash",
        ):
            self.assertIsNotNone(self.get(runner, k), k)
        self.assertEqual(self.get(runner, "boot_id"), "boot-1")
        self.assertTrue(self.get(runner, "final"))
        self.assertEqual(job["gpus"][0]["uuid"], GPUS[1][1])
        # Plain status of a finished job prints its result.json.
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(self.get(st, "_taskmgr", "status"), "succeeded")
        self.assertEqual(self.get(st, "outputs"), {"speedup": 1.5})

    def test_cuda_visible_devices_empty_without_gpus(self) -> None:
        out = os.path.join(self.tmp, "cvd")
        j = self.submit(
            self.make_pkg(
                script="printf '%%s' \"${CUDA_VISIBLE_DEVICES-unset}\" > %s; %s" % (out, OK_RESULT)
            )
        )
        _ = self.wait_state(j, "succeeded")
        with open(out) as f:
            self.assertEqual(f.read(), "")

    def test_failure_modes(self) -> None:
        nz = self.submit(self.make_pkg(script="exit 3"))
        sig = self.submit(self.make_pkg(script="kill -9 $$"))
        inv = self.submit(
            self.make_pkg(script=write_result({"outputs": {"a": 1}}), required=["a", "b"])
        )
        noout = self.submit(self.make_pkg(script=write_result({"x": 1})))
        r = self.wait_state(nz, "failed")
        self.assertEqual(self.get(self.result(nz), "_taskmgr", "return_code"), 3)
        self.assertEqual(self.get(r, "error", "kind"), "exit_code")
        _ = self.wait_state(sig, "failed")
        m = self.obj(self.result(sig), "_taskmgr")
        self.assertEqual((m["return_code"], m["signal"]), (None, 9))
        _ = self.wait_state(inv, "invalid_result")
        m = self.obj(self.result(inv), "_taskmgr")
        self.assertEqual(m["return_code"], 0)
        self.assertTrue(any("'b'" in e for e in self.strs(m, "validation_errors")))
        _ = self.wait_state(noout, "invalid_result")

    def test_result_merge_cases(self) -> None:
        taskwrote = self.submit(
            self.make_pkg(
                script=write_result({"outputs": {}, "_taskmgr": {"status": "fake"}, "keep": 1})
            )
        )
        missing = self.submit(self.make_pkg(script="exit 0"))  # writes no result.json
        corrupt = self.submit(self.make_pkg(script="echo '{not json' > $TASKMGR_RESULT_FILE"))
        clash = self.submit(
            self.make_pkg(
                script="echo task > result.json.corrupt; echo '[1' > $TASKMGR_RESULT_FILE"
            )
        )
        _ = self.wait_state(taskwrote, "succeeded")
        res = self.result(taskwrote)
        self.assertEqual(res["keep"], 1)
        self.assertEqual(self.get(res, "_taskmgr", "status"), "succeeded")
        self.assertIn("task_wrote_taskmgr", self.arr(res, "_taskmgr", "warnings"))
        _ = self.wait_state(missing, "invalid_result")
        res = self.result(missing)
        self.assertEqual(set(res), {"schema_version", "_taskmgr"})
        _ = self.wait_state(corrupt, "invalid_result")
        res = self.result(corrupt)
        self.assertEqual(set(res), {"schema_version", "_taskmgr"})
        self.assertTrue(
            any("corrupt" in e for e in self.strs(res, "_taskmgr", "validation_errors"))
        )
        wd = os.path.join(self.job_dir(corrupt), "workdir")
        with open(os.path.join(wd, "result.json.corrupt")) as f:
            self.assertEqual(f.read(), "{not json\n")
        _ = self.wait_state(clash, "invalid_result")
        wd = os.path.join(self.job_dir(clash), "workdir")
        with open(os.path.join(wd, "result.json.corrupt")) as f:
            self.assertEqual(f.read(), "task\n")  # the task's own file is untouched
        with open(os.path.join(wd, "result.json.corrupt.1")) as f:
            self.assertEqual(f.read(), "[1\n")

    def test_progress_written_by_task_only(self) -> None:
        j = self.submit(
            self.make_pkg(
                script='python3 -c \'import json,os;json.dump({"schema_version":1,"stage":"half",'
                + '"updated_at":"2026-10-29T08:15:00Z","percent":50},'
                + 'open(os.environ["TASKMGR_PROGRESS_FILE"],"w"))\'; sleep 2'
            )
        )

        def progress_half() -> bool:
            progress = self.obj(self.ok("status", j, "--host", "s1")).get("progress") or {}
            return self.obj(progress).get("stage") == "half"

        _ = self.wait_for(progress_half, 15, "progress")
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(self.get(st, "state"), "running")
        self.assertEqual(self.get(st, "run", "status"), "running")
        self.assertNotIn("attempt", self.obj(st, "run"))
        self.assertNotIn("attempt", self.obj(st))
        self.assertEqual((self.get(st, "retry_of"), self.get(st, "retried_as")), (None, []))
        self.assertEqual(self.get(st, "progress", "percent"), 50)
        _ = self.wait_state(j, "invalid_result")

    def test_prestart_check_failures(self) -> None:
        cases = {
            "path": self.make_pkg(extra={"environment": {"required_paths": ["/no/such/path"]}}),
            "venv": self.make_pkg(extra={"environment": {"venv": "/no/such/venv"}}),
            "cmd": self.make_pkg(argv=["no-such-command-xyz"]),
            "noexec": self.make_pkg(argv=["./run.sh"], files={"run.sh": "#!/bin/sh\n"}),
        }
        ids = {k: self.submit(p) for k, p in cases.items()}
        for k, j in ids.items():
            job = self.wait_state(j, "failed")
            self.assertEqual(self.get(job, "error", "kind"), "setup", k)
            m = self.obj(self.result(j), "_taskmgr")
            self.assertIsNone(m["return_code"])
            self.assertEqual(self.get(m, "error", "kind"), "setup")
            self.assertFalse(os.path.exists(os.path.join(self.job_dir(j), "stdout.log")))

    def test_prestart_gpu_disappeared(self) -> None:
        self.stop_daemon()
        j = self.submit(self.make_pkg(gpus=[3]))
        self.set_gpus(GPUS[:3])
        _ = self.start_daemon()
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "setup")
        self.assertIn(GPUS[3][1], self.text(job, "error", "message"))

    def test_bare_command_resolved_on_path(self) -> None:
        j = self.submit(self.make_pkg(argv=["bash", "-c", "exit 0"]))
        _ = self.wait_state(j, "invalid_result")
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 0)
        # A relative entry path must be executable; packed with its exec bit it runs.
        j = self.submit(self._exec_pkg())
        _ = self.wait_state(j, "failed")
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 4)

    def _exec_pkg(self) -> str:
        p = self.make_pkg(argv=["./bin/run.sh"], files={"bin/run.sh": "#!/bin/sh\nexit 4\n"})
        os.chmod(os.path.join(p, "bin", "run.sh"), 0o755)
        return p

    def test_shim_fails_to_start(self) -> None:
        self.stop_daemon()
        j = self.submit(self.make_pkg())
        _ = self.start_daemon(env={"TASKMGR_SHIM_EXECUTABLE": "/no/such/python"})
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "shim_start_failed")
        self.assertIsNone(self.get(self.result(j), "_taskmgr", "return_code"))

    def test_timeout_with_term_and_kill(self) -> None:
        polite = self.submit(self.make_pkg(script="sleep 60", timeout=1))
        stubborn = self.submit(
            self.make_pkg(script="trap '' TERM; while true; do sleep 0.1; done", timeout=1)
        )
        for j in (polite, stubborn):
            job = self.wait_state(j, "timed_out", timeout=20)
            self.assertEqual(job["stop_reason"], "timeout")
            r = self.runner(j)
            self.assertFalse(pid_alive(self.num(r, "leader_pid")))
            self.assertEqual(self.get(self.result(j), "_taskmgr", "status"), "timed_out")

    def test_cancel_queued_running_and_terminal(self) -> None:
        self.stop_daemon()
        q = self.submit(self.make_pkg())
        out = self.obj(self.ok("cancel", q, "--host", "s1"))
        self.assertEqual((out["state"], out["changed"]), ("cancelled", True))
        _ = self.start_daemon()
        r = self.submit(self.make_pkg(script="sleep 60"))
        _ = self.wait_runner(r, "pgid")
        out = self.obj(self.ok("cancel", r, "--host", "s1"))
        self.assertTrue(out["changed"])
        self.assertTrue(self.job(r)["cancel_requested"])
        _ = self.wait_state(r, "cancelled", timeout=20)
        self.assertFalse(pid_alive(self.num(self.runner(r), "leader_pid")))
        out = self.obj(self.ok("cancel", r, "--host", "s1"))
        self.assertEqual((out["state"], out["changed"]), ("cancelled", False))
        time.sleep(1)
        self.assertEqual(self.job(q)["state"], "cancelled")
        self.assertIsNone(self.job(q)["dispatched_at"])  # never ran
        self.assertFalse(os.path.exists(os.path.join(self.job_dir(q), "runner.json")))
        st = self.ok("status", q, "--host", "s1")
        self.assertEqual(self.get(st, "_taskmgr", "status"), "cancelled")

    def test_leftover_processes_are_cleaned(self) -> None:
        marker = os.path.join(self.tmp, "child.pid")
        j = self.submit(self.make_pkg(script="sleep 300 & echo $! > %s; exit 0" % marker))
        _ = self.wait_state(j, "invalid_result", timeout=20)
        with open(marker) as f:
            child = int(f.read())
        self.assertFalse(pid_alive(child))
        self.assertIn("leftover_processes", self.arr(self.result(j), "_taskmgr", "warnings"))
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 0)

    def test_retry_creates_new_job_and_keeps_the_old_one(self) -> None:
        counter = os.path.join(self.tmp, "count")
        pkg = self.make_pkg(script="echo x >> %s; exit 1" % counter)
        j = self.submit(pkg, key="k-retry")
        first = self.wait_state(j, "failed")
        first_result = self.result(j)
        out = self.obj(self.ok("retry", j, "--host", "s1"))
        new = self.text(out, "jobid")
        self.assertNotEqual(new, j)
        self.assertRegex(new, "^[0-9a-f]{16}$")
        self.assertEqual(
            (out["retry_of"], out["state"], out["changed"], out["host"]), (j, "queued", True, "s1")
        )
        self.assertGreater(self.num(out, "enqueue_seq"), first["enqueue_seq"])
        job = self.wait_state(new, "failed")
        with open(counter) as f:
            self.assertEqual(f.read(), "x\nx\n")
        # The new job: same task, package and GPUs, linked back to the old one.
        self.assertEqual(job["retry_of"], j)
        self.assertEqual(job["retried_as"], [])
        self.assertEqual(job["auto_restarts"], 0)
        self.assertEqual(job["enqueue_seq"], out["enqueue_seq"])
        for k in ("task_id", "content_hash", "submission_key", "gpus"):
            self.assertEqual(job[k], first[k], k)
        self.assertEqual(self.get(self.result(new), "_taskmgr", "retry_of"), j)
        self.assertEqual(self.get(self.result(new), "_taskmgr", "jobid"), new)
        # The old job keeps its state, record and result; only retried_as is added.
        old = self.job(j)
        self.assertEqual(old["retried_as"], [new])
        for k in ("state", "error", "enqueue_seq", "ended_at", "dispatched_at"):
            self.assertEqual(old.get(k), first.get(k), k)
        self.assertEqual(self.result(j), first_result)
        self.assertEqual(os.path.basename(os.path.dirname(self.job_dir(j))), "done")
        # Statuses show the links in both directions.
        st = self.obj(self.ok("status", j, "--host", "s1"))
        self.assertEqual((st["retry_of"], st["retried_as"]), (None, [new]))
        st = self.obj(self.ok("status", new, "--host", "s1"))
        self.assertEqual((st["retry_of"], st["retried_as"]), (j, []))
        # Retrying again (either job) adds another link; the key still maps to the original.
        out2 = self.obj(self.ok("retry", j, "--host", "s1"))
        self.assertEqual(self.job(j)["retried_as"], [new, out2["jobid"]])
        _ = self.wait_state(self.text(out2, "jobid"), "failed")
        dup = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "k-retry", pkg))
        self.assertEqual((dup["jobid"], dup["deduplicated"]), (j, True))
        # Retry of a non-terminal job is an error and creates nothing.
        r = self.submit(self.make_pkg(script="sleep 30"))
        _ = self.wait_state(r, "running")
        before = self.jobids()
        rc, out, _ = self.task("retry", r, "--host", "s1")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "not_terminal"))
        self.assertEqual(self.jobids(), before)
        self.assertEqual(self.job(r)["retried_as"], [])

    def test_retry_prints_a_table(self) -> None:
        j = self.submit(self.make_pkg(script="exit 1"))
        _ = self.wait_state(j, "failed")
        rc, out, err = self.task("retry", j, text=True)  # no --host: found by jobid
        self.assertEqual((rc, err), (0, ""))
        assert isinstance(out, str)  # a table, not JSON
        header, row = out.splitlines()
        self.assertEqual(header.split(), ["JOBID", "RETRY_OF", "HOST", "STATE"])
        new = row.split()[0]
        self.assertEqual(row.split(), [new, j, "s1", "queued"])
        self.assertEqual(self.job(new)["retry_of"], j)
        self.assertEqual(header.index("RETRY_OF"), row.index(j))  # columns line up
        rc, colored, _ = self.task("retry", j, text=True, color=True)
        assert isinstance(colored, str)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[1mJOBID", colored)  # bold header
        self.assertIn("\x1b[33mqueued\x1b[0m", colored)  # yellow state
        # Without --detail an error is one line on stderr; stdout stays empty.
        rc, out, err = self.task("retry", "0123456789abcdef", text=True)
        self.assertEqual((rc, out), (2, None))
        self.assertEqual(err, "error: job 0123456789abcdef not found on any server (not_found)\n")
