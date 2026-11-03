"""`task status` says who holds the GPUs a waiting job needs, and who cancelled a job."""

import getpass
import os
import socket
import subprocess
import sys

from taskmgr.common import parse_utc_iso
from taskmgr.records import JsonDict
from taskmgr.tests.helpers import GPUS, Env, kill_quiet


class GpuHoldersTest(Env):
    remote_config: JsonDict = {"gpu_retry_interval_seconds": 0.5}

    def holder(self) -> subprocess.Popen[bytes]:
        """A process outside taskmgr with a long command line; reported on GPU 0."""
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", "--holder-" + "x" * 300]
        )
        self.addCleanup(p.wait)
        self.addCleanup(kill_quiet, p.pid)
        self.set_gpu_apps([(GPUS[0][1], p.pid, 40960), (GPUS[2][1], 999999, 100)])
        return p

    def test_pending_gpu_job_shows_the_holder(self) -> None:
        p = self.holder()
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="exit 75", gpus=[0]))
        _ = self.wait_for(lambda: self.job(j).get("pending_reason"), 20, "pending-gpu")
        st = self.ok("status", j, "--host", "s1")
        holders = self.objs(st, "gpu_holders")
        self.assertEqual(len(holders), 1, holders)  # GPU 2 is not this job's
        h = holders[0]
        self.assertEqual((h["gpu"], h["uuid"], h["pid"]), (0, GPUS[0][1], p.pid))
        self.assertEqual(h["used_memory_mib"], 40960)
        self.assertEqual(h["user"], getpass.getuser())
        cmd = self.text(h, "command")
        self.assertTrue(cmd.startswith(sys.executable + " -c import time"), cmd)
        self.assertIn(" --holder-xxx", cmd)
        self.assertEqual(len(cmd), 200)
        self.assertTrue(cmd.endswith("..."))
        self.assertNotIn("taskmgr_jobid", h)
        rc, text, _ = self.task("status", j, "--host", "s1", text=True)
        self.assertEqual(rc, 0)
        assert isinstance(text, str)
        self.assertIn("gpu_holder", text)
        self.assertIn(
            "GPU 0: pid %d user %s 40.0 GiB: %s" % (p.pid, h["user"], sys.executable), text
        )
        # Nobody on the GPU: an empty list ("none" in the text).
        self.set_gpu_apps([])
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(self.arr(st, "gpu_holders"), [])
        _ = self.ok("cancel", j, "--host", "s1")
        _ = self.wait_state(j, "cancelled")

    def test_queued_job_shows_holders_and_a_pid_of_another_namespace(self) -> None:
        # No daemon: the job stays queued on GPU 2, which a pid unknown here holds.
        self.set_gpu_apps([(GPUS[2][1], 999999, 100)])
        j = self.submit(self.make_pkg(gpus=[2]))
        h = self.objs(self.ok("status", j, "--host", "s1"), "gpu_holders")
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["pid"], h[0]["user"], h[0]["command"]), (999999, None, None))
        # A CPU job waits for no GPU: no holder list.
        c = self.submit(self.make_pkg())
        self.assertIsNone(self.get(self.ok("status", c, "--host", "s1"), "gpu_holders"))

    def test_broken_nvidia_smi_is_reported_not_fatal(self) -> None:
        j = self.submit(self.make_pkg(gpus=[1]))
        with open(self.smi, "w") as f:
            _ = f.write("#!/bin/sh\nexit 3\n")
        st = self.ok("status", j, "--host", "s1")
        self.assertIsNone(self.get(st, "gpu_holders"))
        self.assertIn("nvidia-smi", self.text(st, "gpu_holders_error"))


class CancelRecordTest(Env):
    def me(self) -> str:
        return "%s@%s" % (getpass.getuser(), socket.gethostname())

    def test_cli_cancel_records_the_local_user(self) -> None:
        j = self.submit(self.make_pkg())  # no daemon: stays queued
        out = self.ok("cancel", j, "--host", "s1")
        req = self.obj(out, "cancel_request")
        self.assertEqual((req["by"], req["via"]), (self.me(), "cli"))
        self.assertIsNotNone(parse_utc_iso(req["at"]))
        self.assertEqual(self.job(j).get("cancel_request"), req)
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(self.get(st, "_taskmgr", "status"), "cancelled")
        self.assertEqual(self.obj(st, "cancel_request"), req)
        rc, text, _ = self.task("status", j, "--host", "s1", text=True)
        assert isinstance(text, str)
        self.assertEqual(rc, 0)
        self.assertIn("cancelled_by: %s at %s" % (self.me(), req["at"]), text)
        self.assertNotIn("cancel_request", text.split("result:")[-1])
        # A second cancel changes nothing and keeps the first request.
        out = self.ok("cancel", j, "--host", "s1")
        self.assertEqual(self.obj(out, "cancel_request"), req)

    def test_running_job_and_server_side_cancel(self) -> None:
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "gate")
        j = self.submit(self.make_pkg(script="while [ ! -f %s ]; do sleep 0.05; done" % gate))
        _ = self.wait_state(j, "running")
        rc, out, _ = self.remote("cancel", j)  # on the server, without --by
        self.assertEqual(rc, 0)
        req = self.obj(out, "cancel_request")
        self.assertEqual((req["by"], req["via"]), (self.me(), "remote"))
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(self.obj(st, "cancel_request"), req)
        _ = self.wait_state(j, "cancelled")
        rc, text, _ = self.task("status", j, "--host", "s1", text=True)
        assert isinstance(text, str)
        self.assertIn("(on the server)", text)

    def test_server_without_the_feature_gets_no_by(self) -> None:
        j = self.submit(self.make_pkg())
        old = {"TASKMGR_TEST_FEATURES": "-"}  # the client must not send --by
        rc, out, err = self.task("cancel", j, "--host", "s1", env=old)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.get(out, "cancel_request", "via"), "remote")
