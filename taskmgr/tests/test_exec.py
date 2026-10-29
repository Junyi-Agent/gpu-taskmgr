"""Execution outcomes, environment, result merge, pre-start checks, stop procedure, retry."""

import json
import os
import time

from helpers import GPUS, OK_RESULT, Env, pid_alive

PY_RESULT = ("python3 -c 'import json,os;"
             "json.dump(%s, open(os.environ[\"TASKMGR_RESULT_FILE\"], \"w\"))'")


def write_result(obj):
    return PY_RESULT % json.dumps(obj).replace("'", "")


class ExecTest(Env):
    def setUp(self):
        super().setUp()
        self.start_daemon(env={"DAEMON_ONLY_VAR": "leak"})

    def test_success_and_environment(self):
        venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(venv, "bin"))
        self.set_config(base_env={"LANG": "en_US.UTF-8", "EXTRA_BASE": "1"})
        out = os.path.join(self.tmp, "env.json")
        script = ("python3 -c 'import json,os,sys; json.dump(dict(os.environ), open(\"%s\", \"w\"));"
                  "sys.stdout.write(\"hello\\n\"); sys.stderr.write(\"oops\\n\")';"
                  % out) + write_result({"outputs": {"speedup": 1.5}, "notes": "kept"})
        pkg = self.make_pkg(script=script, gpus=[1], required=["speedup"],
                            env={"NCCL_DEBUG": "WARN", "LANG": "C"},
                            extra={"environment": {"venv": venv, "required_paths": [self.tmp]}})
        j = self.submit(pkg)
        job = self.wait_state(j, "succeeded")
        res = self.result(j)
        self.assertEqual(res["outputs"], {"speedup": 1.5})
        self.assertEqual(res["notes"], "kept")
        m = res["_taskmgr"]
        self.assertEqual((m["status"], m["return_code"], m["signal"], m["final"]),
                         ("succeeded", 0, None, True))
        for k in ("jobid", "task_id", "attempt", "host", "gpus", "started_at", "ended_at",
                  "validation_errors", "error", "warnings", "paths"):
            self.assertIn(k, m)
        self.assertEqual(m["gpus"], [{"declared": 1, "uuid": GPUS[1][1]}])
        with open(out) as f:
            env = json.load(f)
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
        self.assertEqual(env["TASKMGR_ATTEMPT"], "1")
        # While running, the job lives in in-progress/; afterwards in done/.
        wd = os.path.join(self.root(), "in-progress", j, "attempts", "0001", "workdir")
        self.assertEqual(env["TASKMGR_WORKDIR"], wd)
        final_wd = os.path.join(self.root(), "done", j, "attempts", "0001", "workdir")
        self.assertEqual(m["paths"]["workdir"], final_wd)
        self.assertEqual(m["paths"]["artifacts"], os.path.join(final_wd, "artifacts"))
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
        for k in ("boot_id", "shim_pid", "shim_start_time", "pgid", "leader_pid",
                  "leader_start_time", "argv_hash"):
            self.assertIsNotNone(runner[k], k)
        self.assertEqual(runner["boot_id"], "boot-1")
        self.assertTrue(runner["final"])
        self.assertEqual(job["gpus"][0]["uuid"], GPUS[1][1])
        # Plain status of a finished job prints its result.json.
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(st["_taskmgr"]["status"], "succeeded")
        self.assertEqual(st["outputs"], {"speedup": 1.5})

    def test_cuda_visible_devices_empty_without_gpus(self):
        out = os.path.join(self.tmp, "cvd")
        j = self.submit(self.make_pkg(script="printf '%%s' \"${CUDA_VISIBLE_DEVICES-unset}\" > %s; %s"
                                      % (out, OK_RESULT)))
        self.wait_state(j, "succeeded")
        with open(out) as f:
            self.assertEqual(f.read(), "")

    def test_failure_modes(self):
        nz = self.submit(self.make_pkg(script="exit 3"))
        sig = self.submit(self.make_pkg(script="kill -9 $$"))
        inv = self.submit(self.make_pkg(script=write_result({"outputs": {"a": 1}}),
                                        required=["a", "b"]))
        noout = self.submit(self.make_pkg(script=write_result({"x": 1})))
        r = self.wait_state(nz, "failed")
        self.assertEqual(self.result(nz)["_taskmgr"]["return_code"], 3)
        self.assertEqual(r["error"]["kind"], "exit_code")
        self.wait_state(sig, "failed")
        m = self.result(sig)["_taskmgr"]
        self.assertEqual((m["return_code"], m["signal"]), (None, 9))
        self.wait_state(inv, "invalid_result")
        m = self.result(inv)["_taskmgr"]
        self.assertEqual(m["return_code"], 0)
        self.assertTrue(any("'b'" in e for e in m["validation_errors"]))
        self.wait_state(noout, "invalid_result")

    def test_result_merge_cases(self):
        taskwrote = self.submit(self.make_pkg(script=write_result(
            {"outputs": {}, "_taskmgr": {"status": "fake"}, "keep": 1})))
        missing = self.submit(self.make_pkg(script="exit 0"))  # writes no result.json
        corrupt = self.submit(self.make_pkg(script="echo '{not json' > $TASKMGR_RESULT_FILE"))
        clash = self.submit(self.make_pkg(
            script="echo task > result.json.corrupt; echo '[1' > $TASKMGR_RESULT_FILE"))
        self.wait_state(taskwrote, "succeeded")
        res = self.result(taskwrote)
        self.assertEqual(res["keep"], 1)
        self.assertEqual(res["_taskmgr"]["status"], "succeeded")
        self.assertIn("task_wrote_taskmgr", res["_taskmgr"]["warnings"])
        self.wait_state(missing, "invalid_result")
        res = self.result(missing)
        self.assertEqual(set(res), {"schema_version", "_taskmgr"})
        self.wait_state(corrupt, "invalid_result")
        res = self.result(corrupt)
        self.assertEqual(set(res), {"schema_version", "_taskmgr"})
        self.assertTrue(any("corrupt" in e for e in res["_taskmgr"]["validation_errors"]))
        wd = os.path.join(self.job_dir(corrupt), "attempts", "0001", "workdir")
        with open(os.path.join(wd, "result.json.corrupt")) as f:
            self.assertEqual(f.read(), "{not json\n")
        self.wait_state(clash, "invalid_result")
        wd = os.path.join(self.job_dir(clash), "attempts", "0001", "workdir")
        with open(os.path.join(wd, "result.json.corrupt")) as f:
            self.assertEqual(f.read(), "task\n")  # the task's own file is untouched
        with open(os.path.join(wd, "result.json.corrupt.1")) as f:
            self.assertEqual(f.read(), "[1\n")

    def test_progress_written_by_task_only(self):
        j = self.submit(self.make_pkg(
            script="python3 -c 'import json,os;json.dump({\"schema_version\":1,\"stage\":\"half\","
                   "\"updated_at\":\"2026-10-29T08:15:00Z\",\"percent\":50},"
                   "open(os.environ[\"TASKMGR_PROGRESS_FILE\"],\"w\"))'; sleep 2"))
        self.wait_for(lambda: (self.ok("status", j, "--host", "s1").get("progress") or {})
                      .get("stage") == "half", 15, "progress")
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(st["state"], "running")
        self.assertEqual(st["attempt"]["attempt"], 1)
        self.assertEqual(st["progress"]["percent"], 50)
        self.wait_state(j, "invalid_result")

    def test_prestart_check_failures(self):
        cases = {
            "path": self.make_pkg(extra={"environment": {"required_paths": ["/no/such/path"]}}),
            "venv": self.make_pkg(extra={"environment": {"venv": "/no/such/venv"}}),
            "cmd": self.make_pkg(argv=["no-such-command-xyz"]),
            "noexec": self.make_pkg(argv=["./run.sh"], files={"run.sh": "#!/bin/sh\n"}),
        }
        ids = {k: self.submit(p) for k, p in cases.items()}
        for k, j in ids.items():
            job = self.wait_state(j, "failed")
            self.assertEqual(job["error"]["kind"], "setup", k)
            m = self.result(j)["_taskmgr"]
            self.assertIsNone(m["return_code"])
            self.assertEqual(m["error"]["kind"], "setup")
            self.assertFalse(os.path.exists(os.path.join(self.job_dir(j), "attempts", "0001",
                                                         "stdout.log")))

    def test_prestart_gpu_disappeared(self):
        self.stop_daemon()
        j = self.submit(self.make_pkg(gpus=[3]))
        self.set_gpus(GPUS[:3])
        self.start_daemon()
        job = self.wait_state(j, "failed")
        self.assertEqual(job["error"]["kind"], "setup")
        self.assertIn(GPUS[3][1], job["error"]["message"])

    def test_bare_command_resolved_on_path(self):
        j = self.submit(self.make_pkg(argv=["bash", "-c", "exit 0"]))
        self.wait_state(j, "invalid_result")
        self.assertEqual(self.result(j)["_taskmgr"]["return_code"], 0)
        # A relative entry path must be executable; packed with its exec bit it runs.
        j = self.submit(self._exec_pkg())
        self.wait_state(j, "failed")
        self.assertEqual(self.result(j)["_taskmgr"]["return_code"], 4)

    def _exec_pkg(self):
        p = self.make_pkg(argv=["./bin/run.sh"], files={"bin/run.sh": "#!/bin/sh\nexit 4\n"})
        os.chmod(os.path.join(p, "bin", "run.sh"), 0o755)
        return p

    def test_shim_fails_to_start(self):
        self.stop_daemon()
        j = self.submit(self.make_pkg())
        self.start_daemon(env={"TASKMGR_SHIM_EXECUTABLE": "/no/such/python"})
        job = self.wait_state(j, "failed")
        self.assertEqual(job["error"]["kind"], "shim_start_failed")
        self.assertIsNone(self.result(j)["_taskmgr"]["return_code"])

    def test_timeout_with_term_and_kill(self):
        polite = self.submit(self.make_pkg(script="sleep 60", timeout=1))
        stubborn = self.submit(self.make_pkg(
            script="trap '' TERM; while true; do sleep 0.1; done", timeout=1))
        for j in (polite, stubborn):
            job = self.wait_state(j, "timed_out", timeout=20)
            self.assertEqual(job["stop_reason"], "timeout")
            r = self.runner(j)
            self.assertFalse(pid_alive(r["leader_pid"]))
            self.assertEqual(self.result(j)["_taskmgr"]["status"], "timed_out")

    def test_cancel_queued_running_and_terminal(self):
        self.stop_daemon()
        q = self.submit(self.make_pkg())
        out = self.ok("cancel", q, "--host", "s1")
        self.assertEqual((out["state"], out["changed"]), ("cancelled", True))
        self.start_daemon()
        r = self.submit(self.make_pkg(script="sleep 60"))
        self.wait_runner(r, "pgid")
        out = self.ok("cancel", r, "--host", "s1")
        self.assertTrue(out["changed"])
        self.assertTrue(self.job(r)["cancel_requested"])
        self.wait_state(r, "cancelled", timeout=20)
        self.assertFalse(pid_alive(self.runner(r)["leader_pid"]))
        out = self.ok("cancel", r, "--host", "s1")
        self.assertEqual((out["state"], out["changed"]), ("cancelled", False))
        time.sleep(1)
        self.assertEqual(self.job(q)["state"], "cancelled")
        self.assertEqual(self.job(q)["attempts"], [])  # never ran
        st = self.ok("status", q, "--host", "s1")
        self.assertEqual(st["_taskmgr"]["status"], "cancelled")

    def test_leftover_processes_are_cleaned(self):
        marker = os.path.join(self.tmp, "child.pid")
        j = self.submit(self.make_pkg(script="sleep 300 & echo $! > %s; exit 0" % marker))
        self.wait_state(j, "invalid_result", timeout=20)
        with open(marker) as f:
            child = int(f.read())
        self.assertFalse(pid_alive(child))
        self.assertIn("leftover_processes", self.result(j)["_taskmgr"]["warnings"])
        self.assertEqual(self.result(j)["_taskmgr"]["return_code"], 0)

    def test_retry_creates_new_attempt_and_keeps_history(self):
        counter = os.path.join(self.tmp, "count")
        j = self.submit(self.make_pkg(script="echo x >> %s; exit 1" % counter))
        first = self.wait_state(j, "failed")
        rc, out, _ = self.task("retry", j, "--host", "s1")
        self.assertEqual(rc, 0)
        self.assertGreater(out["enqueue_seq"], first["enqueue_seq"])
        self.wait_for(lambda: len(self.job(j)["attempts"]) == 2 and
                      self.job(j)["state"] == "failed", 20, "second attempt")
        with open(counter) as f:
            self.assertEqual(f.read(), "x\nx\n")
        self.assertEqual(self.result(j, 1)["_taskmgr"]["attempt"], 1)
        self.assertEqual(self.result(j, 2)["_taskmgr"]["attempt"], 2)
        # Retry of a non-terminal job is an error.
        r = self.submit(self.make_pkg(script="sleep 30"))
        self.wait_state(r, "running")
        self.assertEqual(self.task("retry", r, "--host", "s1")[0], 2)
