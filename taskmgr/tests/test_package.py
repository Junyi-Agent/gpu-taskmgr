"""Packaging, content hash, validation, init/validate exit codes (Sections 2, 5.1, 6.1, 16)."""

import io
import json
import os
import tarfile
import time

from helpers import Env

from taskmgr import package
from taskmgr.common import JsonDict


TarEntry = tuple[JsonDict, "bytes | None"]


def write_tar(path: str, entries: list[TarEntry]) -> None:
    """entries: list of (TarInfo kwargs, data or None)."""
    with tarfile.open(path, "w:gz") as tar:
        for kw, data in entries:
            ti = tarfile.TarInfo(kw.pop("name"))
            for k, v in kw.items():
                setattr(ti, k, v)
            if data is not None:
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
            else:
                tar.addfile(ti)


class PackageTest(Env):
    def test_pack_is_deterministic_and_hash_matches_archive(self) -> None:
        pkg = self.make_pkg(files={"src/a.py": "print(1)\n", "z.txt": "z"})
        os.symlink("src/a.py", os.path.join(pkg, "link.py"))
        os.chmod(os.path.join(pkg, "src/a.py"), 0o755)
        a, b = os.path.join(self.tmp, "a.tgz"), os.path.join(self.tmp, "b.tgz")
        package.pack_dir(pkg, a)
        os.utime(os.path.join(pkg, "z.txt"), (1, 1))  # mtime must not matter
        time.sleep(1.1)
        package.pack_dir(pkg, b)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        with tarfile.open(a) as t:
            members = t.getmembers()
        self.assertEqual([m.name for m in members], sorted(m.name for m in members))
        for m in members:
            self.assertEqual((m.mtime, m.uid, m.gid, m.uname, m.gname), (0, 0, 0, "", ""))
        with open(a, "rb") as f:
            self.assertEqual(f.read(10)[4:8], b"\0\0\0\0")  # gzip header mtime 0
        h_dir = package.content_hash(pkg)
        ext = os.path.join(self.tmp, "ext")
        package.safe_extract(a, ext)
        self.assertEqual(package.content_hash(ext), h_dir)
        # The CLI reports the same hash for the directory and an archive of it.
        rc1, v1, _ = self.task("validate", pkg)
        rc2, v2, _ = self.task("validate", a)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(v1["content_hash"], v2["content_hash"])

    def test_hash_sees_exec_bit_and_content(self) -> None:
        pkg = self.make_pkg(files={"x.sh": "echo\n"})
        h1 = package.content_hash(pkg)
        os.chmod(os.path.join(pkg, "x.sh"), 0o755)
        h2 = package.content_hash(pkg)
        self.assertNotEqual(h1, h2)

    def test_init_and_validate_exit_codes(self) -> None:
        target = os.path.join(self.tmp, "AA-01")
        out = self.ok("init", "AA-01", "--output", target)
        self.assertEqual(out["path"], target)
        for f in ("task.json", "TASK.md", "progress.json", "taskmgr_task.py", "run.py"):
            self.assertTrue(os.path.isfile(os.path.join(target, f)), f)
        rc, out, _ = self.task("init", "AA-01", "--output", target)
        self.assertEqual(rc, 2)
        self.assertIn("error", out)
        # Fresh template still has placeholders: validation fails with exit 1.
        rc, out, _ = self.task("validate", target)
        self.assertEqual(rc, 1)
        self.assertFalse(out["valid"])
        self.assertTrue(any(e["code"] == "placeholder" for e in out["errors"]))
        rc, out, _ = self.task("validate", os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 2)
        rc, out, _ = self.task("init", "bad-id")
        self.assertEqual(rc, 2)
        # Filling in the template makes it valid.
        with open(os.path.join(target, "task.json")) as f:
            t = json.load(f)
        t["gpus"] = []
        t["environment"] = {"required_paths": []}
        t["result"] = {"required_outputs": ["example"]}
        with open(os.path.join(target, "task.json"), "w") as f:
            json.dump(t, f)
        with open(os.path.join(target, "TASK.md")) as f:
            md = f.read()
        import re

        with open(os.path.join(target, "TASK.md"), "w") as f:
            f.write(re.sub(r"<REQUIRED[^>]*>", "filled", md))
        rc, out, _ = self.task("validate", target)
        self.assertEqual(
            (rc, out),
            (0, {"valid": True, "task_id": "AA-01", "content_hash": out.get("content_hash")}),
        )

    def test_examples_are_valid(self) -> None:
        ex = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")
        for name in os.listdir(ex):
            rc, out, err = self.task("validate", os.path.join(ex, name))
            self.assertEqual(rc, 0, (name, out, err))

    def test_invalid_packages_rejected(self) -> None:
        cases: list[tuple[JsonDict, str]] = [
            ({"extra": {"task_id": "A-1"}}, "task_id"),
            ({"extra": {"task_id": "aa-01"}}, "task_id"),
            ({"argv": ["./missing.sh"]}, "entry"),
            ({"argv": ["../outside.sh"]}, "entry"),
            ({"env": {"CUDA_VISIBLE_DEVICES": "0"}}, "env"),
            ({"env": {"TASKMGR_X": "0"}}, "env"),
            ({"gpus": [0, 0]}, "gpu"),
            ({"gpus": ["MIG-abc"]}, "gpu"),
            ({"extra": {"timeout_seconds": 0}}, "schema"),
        ]
        for kw, code in cases:
            pkg = self.make_pkg(**kw)
            rc, out, _ = self.task("validate", pkg)
            self.assertEqual(rc, 1, kw)
            self.assertIn(code, [e["code"] for e in out["errors"]], (kw, out))
        pkg = self.make_pkg()
        os.unlink(os.path.join(pkg, "TASK.md"))
        rc, out, _ = self.task("validate", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("missing_file", [e["code"] for e in out["errors"]])
        pkg = self.make_pkg()
        with open(os.path.join(pkg, "progress.json"), "w") as f:
            f.write('{"schema_version": 1, "stage": "x"}')
        self.assertEqual(self.task("validate", pkg)[0], 1)
        pkg = self.make_pkg()
        with open(os.path.join(pkg, "TASK.md"), "a") as f:
            f.write("\n<REQUIRED: fill me>\n")
        self.assertEqual(self.task("validate", pkg)[0], 1)

    def test_symlinks_and_special_files(self) -> None:
        pkg = self.make_pkg()
        os.symlink("/etc/passwd", os.path.join(pkg, "abs"))
        rc, out, _ = self.task("validate", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("symlink", [e["code"] for e in out["errors"]])
        pkg = self.make_pkg()
        os.makedirs(os.path.join(pkg, "d"))
        os.symlink("../../x", os.path.join(pkg, "d", "up"))
        self.assertEqual(self.task("validate", pkg)[0], 1)
        pkg = self.make_pkg()
        os.mkfifo(os.path.join(pkg, "fifo"))
        rc, out, _ = self.task("validate", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("special_file", [e["code"] for e in out["errors"]])
        pkg = self.make_pkg(files={"d/f": "x"})
        os.symlink("d/f", os.path.join(pkg, "ok"))  # internal relative link is fine
        self.assertEqual(self.task("validate", pkg)[0], 0)

    def test_command_name_entry_passes_validation(self) -> None:
        pkg = self.make_pkg(argv=["bash", "-c", "exit 0"])
        self.assertEqual(self.task("validate", pkg)[0], 0)
        pkg = self.make_pkg(argv=["definitely-not-a-command-xyz"])
        self.assertEqual(self.task("validate", pkg)[0], 0)  # checked remotely

    def _tar_with(self, extra_entries: list[TarEntry]) -> str:
        good = self.make_pkg()
        entries: list[TarEntry] = []
        for name in ("task.json", "TASK.md", "progress.json"):
            with open(os.path.join(good, name), "rb") as f:
                entries.append(({"name": name, "mode": 0o644}, f.read()))
        path = os.path.join(self.tmp, "bad-%d.tgz" % len(os.listdir(self.tmp)))
        write_tar(path, entries + extra_entries)
        return path

    def test_unsafe_archives_rejected_locally_and_remotely(self) -> None:
        bad: list[list[TarEntry]] = [
            [({"name": "../evil", "mode": 0o644}, b"x")],
            [({"name": "/abs/evil", "mode": 0o644}, b"x")],
            [({"name": "lnk", "type": tarfile.SYMTYPE, "linkname": "../../etc"}, None)],
            [({"name": "lnk", "type": tarfile.SYMTYPE, "linkname": "/etc/passwd"}, None)],
            [({"name": "hl", "type": tarfile.LNKTYPE, "linkname": "task.json"}, None)],
            [({"name": "dev", "type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}, None)],
            [({"name": "fifo", "type": tarfile.FIFOTYPE}, None)],
        ]
        self.assertTrue(os.path.exists(self._tar_with([])))
        self.assertEqual(self.task("validate", self._tar_with([]))[0], 0)
        for extra in bad:
            path = self._tar_with(extra)
            rc, out, _ = self.task("validate", path)
            self.assertEqual(rc, 1, (extra, out))
            self.assertFalse(out["valid"])
            # The remote side refuses it independently of the local check.
            with open(path, "rb") as f:
                rc, out, _ = self.remote("receive", stdin=f)
            self.assertEqual(rc, 2, (extra, out))
            self.assertEqual(out["error"]["code"], "unsafe_package")
        self.assertEqual(os.listdir(os.path.join(self.root(), "queue")), [])
        self.assertEqual(os.listdir(os.path.join(self.root(), "incoming")), [])

    def test_remote_rejects_oversized_package(self) -> None:
        self.set_config(max_package_bytes=1000)
        pkg = self.make_pkg(files={"big.bin": "x" * 5000})
        rc, out, _ = self.task("submit", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(out["error"]["code"], "package_too_large")

    def test_remote_rejects_unknown_gpu(self) -> None:
        pkg = self.make_pkg(gpus=[7])
        rc, out, _ = self.task("submit", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(out["error"]["code"], "invalid_gpus")
        pkg = self.make_pkg(gpus=["GPU-doesnotexist"])
        self.assertEqual(self.task("submit", "--host", "s1", pkg)[0], 2)
        # Index and UUID naming the same device are duplicates.
        pkg = self.make_pkg(gpus=[0, "GPU-00000000-0000-0000-0000-000000000000"])
        rc, out, _ = self.task("submit", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", out["error"]["message"])

    def test_wrapped_archive_with_top_level_directory(self) -> None:
        pkg = self.make_pkg()
        path = os.path.join(self.tmp, "wrapped.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            tar.add(pkg, arcname="AA-01")
        out = self.ok("submit", "--host", "s1", path)
        self.assertEqual(out["task_id"], "AA-01")
        self.assertEqual(out["content_hash"], package.content_hash(pkg))

    def test_symlink_chain_escaping_package_is_rejected(self) -> None:
        pkg = self.make_pkg()
        os.symlink(".", os.path.join(pkg, "x"))
        os.symlink("x/..", os.path.join(pkg, "y"))  # lexically ".", physically the parent
        rc, out, _ = self.task("validate", pkg)
        self.assertEqual(rc, 1)
        self.assertEqual([e["path"] for e in out["errors"] if e["code"] == "symlink"], ["y"])
        path = self._tar_with(
            [
                ({"name": "x", "type": tarfile.SYMTYPE, "linkname": "."}, None),
                ({"name": "y", "type": tarfile.SYMTYPE, "linkname": "x/.."}, None),
            ]
        )
        self.assertEqual(self.task("validate", path)[0], 1)
        with open(path, "rb") as f:
            rc, out, _ = self.remote("receive", stdin=f)
        self.assertEqual((rc, out["error"]["code"]), (2, "unsafe_package"))

    def test_validate_archive_reports_all_errors(self) -> None:
        path = self._tar_with(
            [({"name": "../a", "mode": 0o644}, b"x"), ({"name": "/b", "mode": 0o644}, b"x")]
        )
        rc, out, _ = self.task("validate", path)
        self.assertEqual(rc, 1)
        self.assertEqual(len([e for e in out["errors"] if e["code"] == "unsafe_package"]), 2)
        junk = os.path.join(self.tmp, "junk.tar.gz")
        with open(junk, "w") as f:
            f.write("not an archive")
        rc, out, _ = self.task("validate", junk)
        self.assertEqual((rc, out["errors"][0]["code"]), (1, "bad_archive"))
        rc, out, _ = self.task("validate", "--", "\n")
        self.assertEqual(rc, 2)

    def test_regexes_reject_trailing_newline(self) -> None:
        from taskmgr.common import SUBMISSION_KEY_RE, TASK_ID_RE

        self.assertIsNone(TASK_ID_RE.match("AA-01\n"))
        self.assertIsNone(SUBMISSION_KEY_RE.match("k\n"))
