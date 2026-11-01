"""Submission, server selection, idempotency, protocol version, incoming cleanup."""

import concurrent.futures
import json
import os
import subprocess
import sys
import time
from typing import cast

from taskmgr import package
from taskmgr.records import JsonValue
from taskmgr.tests.helpers import REMOTE, Env


class SubmitTest(Env):
    servers: tuple[str, ...] = ("s1", "s2", "s3")

    def test_directory_and_archive_submission(self) -> None:
        pkg = self.make_pkg()
        a = self.obj(self.ok("submit", "--host", "s1", pkg))
        tgz = os.path.join(self.tmp, "p.tar.gz")
        package.pack_dir(pkg, tgz)
        b = self.obj(self.ok("submit", "--host", "s1", tgz))
        for r in (a, b):
            self.assertEqual(
                set(r) >= {"host", "jobid", "task_id", "submission_key", "deduplicated"}, True
            )
            self.assertEqual(r["host"], "s1")
            self.assertFalse(r["deduplicated"])
            self.assertRegex(self.text(r, "jobid"), r"^[0-9a-f]{16}$")
        self.assertNotEqual(a["jobid"], b["jobid"])  # no key: independent submissions
        self.assertEqual(a["content_hash"], b["content_hash"])
        job = self.job(self.text(a, "jobid"))
        self.assertEqual(job["state"], "queued")
        jd = self.job_dir(self.text(a, "jobid"))
        self.assertTrue(os.path.isfile(os.path.join(jd, "package.tar.gz")))
        self.assertTrue(os.path.isfile(os.path.join(jd, "package", "task.json")))
        # The original local directory is untouched.
        self.assertEqual(sorted(os.listdir(pkg)), ["TASK.md", "progress.json", "task.json"])

    def test_round_robin_skips_disabled_and_host_overrides(self) -> None:
        self.machines[1]["enabled"] = False
        self.write_machines()
        pkg = self.make_pkg()
        hosts = [self.get(self.ok("submit", pkg), "host") for _ in range(4)]
        self.assertEqual(hosts, ["s1", "s3", "s1", "s3"])
        # --host may target a disabled server.
        self.assertEqual(self.get(self.ok("submit", "--host", "s2", pkg), "host"), "s2")
        self.assertEqual(
            self.get(self.ok("submit", pkg), "host"), "s1"
        )  # cursor not consumed by --host
        rc, _, _ = self.task("submit", "--host", "nope", pkg)
        self.assertEqual(rc, 2)

    def test_round_robin_under_concurrent_clis(self) -> None:
        pkg = self.make_pkg()
        n = 12

        def submit(_: int) -> tuple[int, JsonValue, str]:
            return self.task("submit", pkg)

        with concurrent.futures.ThreadPoolExecutor(n) as ex:
            res = list(ex.map(submit, range(n)))
        hosts = [self.text(r[1], "host") for r in res if r[0] == 0]
        self.assertEqual(len(hosts), n)
        self.assertEqual({h: hosts.count(h) for h in set(hosts)}, {"s1": 4, "s2": 4, "s3": 4})
        with open(os.path.join(self.home, "rr.json")) as f:
            self.assertEqual(self.get(cast(JsonValue, json.load(f)), "cursor"), n)

    def test_submission_key_rules(self) -> None:
        pkg = self.make_pkg()
        rc, out, _ = self.task("submit", "--submission-key", "k1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "bad_args")
        rc, _, _ = self.task("submit", "--host", "s1", "--submission-key", "bad key!", pkg)
        self.assertEqual(rc, 2)
        a = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "k1", pkg))
        self.assertFalse(a["deduplicated"])
        tgz = os.path.join(self.tmp, "p.tgz")
        package.pack_dir(pkg, tgz)
        b = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "k1", tgz))
        self.assertTrue(b["deduplicated"])
        self.assertEqual(a["jobid"], b["jobid"])
        self.assertEqual(len(os.listdir(os.path.join(self.root("s1"), "queue"))), 1)
        # Dedup holds regardless of the job state.
        _ = self.ok("cancel", self.text(a, "jobid"), "--host", "s1")
        c = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "k1", pkg))
        self.assertEqual((c["jobid"], c["deduplicated"]), (a["jobid"], True))
        # Same key, different content: error.
        other = self.make_pkg(script="exit 1")
        rc, out, _ = self.task("submit", "--host", "s1", "--submission-key", "k1", other)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "submission_key_conflict")
        # The same key on a different server is unrelated.
        d = self.obj(self.ok("submit", "--host", "s2", "--submission-key", "k1", other))
        self.assertFalse(d["deduplicated"])

    def test_concurrent_submissions(self) -> None:
        pkg = self.make_pkg()

        def submit_s1(_: int) -> tuple[int, JsonValue, str]:
            return self.task("submit", "--host", "s1", pkg)

        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            res = list(ex.map(submit_s1, range(8)))
        ids = [self.text(r[1], "jobid") for r in res]
        self.assertEqual(len(set(ids)), 8)
        seqs = sorted(self.job(j)["enqueue_seq"] for j in ids)
        self.assertEqual(seqs, list(range(1, 9)))
        # Concurrent retries with one key enqueue exactly one job.

        def submit_s2_same_key(_: int) -> tuple[int, JsonValue, str]:
            return self.task("submit", "--host", "s2", "--submission-key", "same", pkg)

        with concurrent.futures.ThreadPoolExecutor(6) as ex:
            res = list(
                ex.map(
                    submit_s2_same_key,
                    range(6),
                )
            )
        self.assertEqual({r[0] for r in res}, {0})
        self.assertEqual(len({self.text(r[1], "jobid") for r in res}), 1)
        self.assertEqual(sum(1 for r in res if not self.get(r[1], "deduplicated")), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.root("s2"), "queue"))), 1)

    def test_uncertain_outcome_reports_retry_command(self) -> None:
        self.machines[0]["local"] = {"fail": "ssh: connect timed out"}
        self.write_machines()
        pkg = self.make_pkg()
        rc, out, err = self.task("submit", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "submission_uncertain")
        self.assertEqual(self.get(out, "error", "host"), "s1")
        key = self.text(out, "error", "details", "submission_key")
        self.assertIn("task submit --host s1 --submission-key %s" % key, err)
        # Round-robin failures are not resubmitted elsewhere.
        rc, out, err = self.task("submit", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "host"), "s1")
        for s in ("s2", "s3"):
            self.assertFalse(
                os.listdir(os.path.join(self.root(s))) != ["config.json"]
                and os.listdir(os.path.join(self.root(s), "queue"))
            )

    def test_dedup_answers_before_machine_dependent_checks(self) -> None:
        pkg = self.make_pkg(gpus=[1])
        a = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "gpu-k", pkg))
        self.set_gpus([])  # the GPU vanished (or nvidia-smi broke) after the first enqueue
        b = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "gpu-k", pkg))
        self.assertEqual((b["jobid"], b["deduplicated"]), (a["jobid"], True))

    def test_retry_after_lost_ack(self) -> None:
        pkg = self.make_pkg()
        # Simulate: the server enqueued the job but the client never saw the ACK.
        tgz = os.path.join(self.tmp, "p.tgz")
        package.pack_dir(pkg, tgz)
        with open(tgz, "rb") as f:
            rc, ack, _ = self.remote("receive", "--submission-key", "lost-1", stdin=f)
        self.assertEqual(rc, 0)
        r = self.obj(self.ok("submit", "--host", "s1", "--submission-key", "lost-1", pkg))
        self.assertEqual((r["jobid"], r["deduplicated"]), (self.get(ack, "jobid"), True))

    def test_protocol_version_mismatch(self) -> None:
        # The remote refuses a client of another version and names its own.
        rc, out, _ = self.remote("--protocol-version", "99", "jobs")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "protocol_mismatch"))
        self.assertEqual(self.get(out, "protocol_version"), 2)
        rc, out, _ = self.task("jobs", "--host", "s1", env={"TASKMGR_TEST_PROTOCOL_VERSION": "99"})
        # jobs lists every target; the per-server error carries the reason.
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "all_hosts_failed"))
        self.assertEqual(self.get(out, "errors", 0, "code"), "protocol_mismatch")
        # Protocol 1 (attempt-based) clients and remotes are rejected in both directions.
        for verb in (["jobs"], ["retry", "0123456789abcdef"], ["logs", "0123456789abcdef"]):
            rc, out, err = self.remote("--protocol-version", "1", *verb)
            self.assertEqual(rc, 2, verb)
            self.assertIn("protocol_mismatch", "%s %s" % (out, err))
        rc, out, _ = self.task(
            "status", "0123456789abcdef", "--host", "s1", env={"TASKMGR_TEST_PROTOCOL_VERSION": "1"}
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "protocol_mismatch")
        rc, out, _ = self.task(
            "retry",
            "0123456789abcdef",
            "--host",
            "s1",
            env={"TASKMGR_TEST_PROTOCOL_VERSION": "1"},
        )
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "protocol_mismatch"))
        pkg = self.make_pkg()
        rc, out, _ = self.task(
            "submit", "--host", "s1", pkg, env={"TASKMGR_TEST_PROTOCOL_VERSION": "1"}
        )
        self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(self.root()), ["config.json"])  # never reached receive

    def test_client_refuses_a_reply_of_another_protocol_version(self) -> None:
        """The client checks protocol_version itself: a remote that answers with another
        version is refused even when the reply is otherwise well-formed (query), and an
        error from a streaming command reports the mismatch instead (logs)."""
        fake = os.path.join(self.tmp, "fake-remote")
        error = '{"protocol_version": 1, "error": {"code": "not_found", "message": "gone"}}'
        reply = '{"protocol_version": 1, "jobs": [], "complete": true}'
        with open(fake, "w") as f:
            _ = f.write(
                "#!/bin/sh\n"
                + 'case " $* " in *" logs "*) echo \'%s\' >&2; exit 2;; esac\n' % error
                + "echo '%s'\n" % reply
            )
        os.chmod(fake, 0o755)
        self.server("s1")["remote_command"] = fake
        self.write_machines()
        rc, out, err = self.task("jobs", "--host", "s1")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.get(out, "error", "code"), "all_hosts_failed", err)
        self.assertEqual(self.get(out, "errors", 0, "code"), "protocol_mismatch")
        rc, out, err = self.task("status", "0123456789abcdef", "--host", "s1")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "protocol_mismatch"), err)
        rc, _, err = self.task("logs", "0123456789abcdef", "--host", "s1")
        self.assertEqual(rc, 2)
        self.assertIn("protocol_mismatch", err)
        self.assertNotIn("not_found", err)

    def test_daemon_restart_keeps_in_progress_upload(self) -> None:
        pkg = self.make_pkg()
        tgz = os.path.join(self.tmp, "p.tgz")
        package.pack_dir(pkg, tgz)
        incoming = os.path.join(self.root(), "incoming")
        os.makedirs(incoming, exist_ok=True)
        stale = os.path.join(incoming, "up-stale")
        os.makedirs(stale)
        old = time.time() - 7200
        os.utime(stale, (old, old))
        env = dict(self.env, TASKMGR_TEST_RECEIVE_PAUSE="4")
        with open(tgz, "rb") as f:
            p = subprocess.Popen(
                [sys.executable, REMOTE, "--root", self.root(), "receive"],
                stdin=f,
                stdout=subprocess.PIPE,
                env=env,
            )
        live = self.wait_for(
            lambda: [d for d in os.listdir(incoming) if d != "up-stale"], 10, "upload dir"
        )
        live = os.path.join(incoming, live[0])
        os.utime(live, (old, old))  # looks old, but it is locked
        _ = self.start_daemon()
        _ = self.wait_for(lambda: not os.path.exists(stale), 10, "stale upload removal")
        self.assertTrue(os.path.isdir(live))
        self.stop_daemon()
        _ = self.start_daemon()
        time.sleep(0.5)
        self.assertTrue(os.path.isdir(live))
        out, _ = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0)
        jobid = self.text(cast(JsonValue, json.loads(out)), "jobid")
        _ = self.wait_state(jobid, "succeeded")

    def test_receive_stops_reading_an_oversized_upload(self) -> None:
        self.set_config(max_package_bytes=1000)
        blob = os.path.join(self.tmp, "blob.bin")
        with open(blob, "wb") as f:
            _ = f.write(os.urandom(3 << 20))  # incompressible: the archive stays > 2 MiB
        with open(blob, "rb") as f:
            rc, out, _ = self.remote("receive", stdin=f)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "package_too_large")
        self.assertIn("upload of more than", self.text(out, "error", "message"))
        self.assertEqual(os.listdir(os.path.join(self.root(), "incoming")), [])

    def test_retry_links_the_read_only_package(self) -> None:
        old = self.submit(self.make_pkg())
        _ = self.ok("cancel", old, "--host", "s1")  # queued -> cancelled, no daemon needed
        new = self.text(self.ok("retry", old, "--host", "s1"), "jobid")
        self.assertEqual(self.job(new)["retry_of"], old)
        self.assertEqual(self.job(old)["retried_as"], [new])
        for rel in ("package.tar.gz", os.path.join("package", "task.json")):
            a = os.stat(os.path.join(self.job_dir(old), rel))
            b = os.stat(os.path.join(self.job_dir(new), rel))
            self.assertEqual((a.st_ino, a.st_dev), (b.st_ino, b.st_dev), rel)
        st = os.stat(os.path.join(self.job_dir(new), "package", "task.json"))
        self.assertEqual(st.st_mode & 0o222, 0)
        self.assertEqual(os.listdir(os.path.join(self.root(), "incoming")), [])
        # Removing a staging copy leaves the shared files read-only.
        from taskmgr.store import remove_tree, stage_package

        updir = os.path.join(self.root(), "incoming", "probe")
        os.mkdir(updir)
        stage_package(self.job_dir(new), updir)
        remove_tree(updir)
        self.assertFalse(os.path.exists(updir))
        st = os.stat(os.path.join(self.job_dir(new), "package", "task.json"))
        self.assertEqual(st.st_mode & 0o222, 0)
