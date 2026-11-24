"""Packaging, content hash, validation, init/validate exit codes (Sections 2, 5.1, 6.1, 16)."""

import io
import json
import os
import tarfile
import time
from typing import TypedDict, cast

from taskmgr import package
from taskmgr.common import TaskmgrError
from taskmgr.records import JsonDict, JsonValue
from taskmgr.tests.helpers import Env

TarEntry = tuple[dict[str, object], "bytes | None"]


class PkgKw(TypedDict, total=False):
    """The make_pkg keyword arguments test_invalid_packages_rejected varies."""

    extra: JsonDict
    argv: list[str]
    env: dict[str, str]
    gpus: list[int | str]


def write_tar(path: str, entries: list[TarEntry]) -> None:
    """entries: list of (TarInfo kwargs, data or None)."""
    with tarfile.open(path, "w:gz") as tar:
        for kw, data in entries:
            name = kw.pop("name")
            assert isinstance(name, str)
            ti = tarfile.TarInfo(name)
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
        _ = package.safe_extract(a, ext)
        self.assertEqual(package.content_hash(ext), h_dir)
        # The CLI reports the same hash for the directory and an archive of it.
        rc1, v1, _ = self.task("validate", "package", pkg)
        rc2, v2, _ = self.task("validate", "package", a)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(self.get(v1, "content_hash"), self.get(v2, "content_hash"))

    def test_hash_sees_exec_bit_and_content(self) -> None:
        """Each change to what the package holds gives another hash: the exec bit, one byte
        of a file, a file name, a symlink target."""
        pkg = self.make_pkg(files={"x.sh": "echo a\n"})
        os.symlink("x.sh", os.path.join(pkg, "link"))
        x = os.path.join(pkg, "x.sh")
        seen = [package.content_hash(pkg)]

        def changed(what: str) -> None:
            h = package.content_hash(pkg)
            self.assertNotIn(h, seen, what)
            seen.append(h)

        os.chmod(x, 0o755)
        changed("exec bit")
        with open(x, "w") as f:
            _ = f.write("echo b\n")  # same size, one byte differs
        changed("content")
        os.rename(x, os.path.join(pkg, "y.sh"))
        changed("file name")
        os.unlink(os.path.join(pkg, "link"))
        os.symlink("y.sh", os.path.join(pkg, "link"))
        changed("symlink target")

    def test_init_and_validate_exit_codes(self) -> None:
        target = os.path.join(self.tmp, "AA-01")
        out = self.ok("create", "package", "AA-01", "--dest", target)
        self.assertEqual(self.get(out, "path"), target)
        for f in ("task.json", "TASK.md", "progress.json", "taskmgr_task.py", "run.py"):
            self.assertTrue(os.path.isfile(os.path.join(target, f)), f)
        rc, out, _ = self.task("create", "package", "AA-01", "--dest", target)
        self.assertEqual(rc, 2)
        self.assertIn("error", self.obj(out))
        # Fresh template still has placeholders: validation fails with exit 1.
        rc, out, _ = self.task("validate", "package", target)
        self.assertEqual(rc, 1)
        self.assertFalse(self.get(out, "valid"))
        self.assertTrue(any(e["code"] == "placeholder" for e in self.objs(out, "errors")))
        rc, out, _ = self.task("validate", "package", os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 2)
        rc, out, _ = self.task("create", "package", "bad-id")
        self.assertEqual(rc, 2)
        # Filling in the template makes it valid.
        with open(os.path.join(target, "task.json")) as f:
            t = self.obj(cast(JsonValue, json.load(f)))
        t["gpus"] = []
        del t["precompile"]
        t["environment"] = {"required_paths": []}
        t["result"] = {"required_outputs": ["example"]}
        with open(os.path.join(target, "task.json"), "w") as f:
            json.dump(t, f)
        with open(os.path.join(target, "TASK.md")) as f:
            md = f.read()
        import re

        with open(os.path.join(target, "TASK.md"), "w") as f:
            _ = f.write(re.sub(r"<REQUIRED[^>]*>", "filled", md))
        rc, out, _ = self.task("validate", "package", target)
        self.assertEqual(
            (rc, out),
            (
                0,
                {
                    "valid": True,
                    "task_id": "AA-01",
                    "content_hash": self.obj(out).get("content_hash"),
                    "features": [],
                    "warnings": [],
                },
            ),
        )

    def test_examples_are_valid(self) -> None:
        ex = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")
        for name in os.listdir(ex):
            rc, out, err = self.task("validate", "package", os.path.join(ex, name))
            self.assertEqual(rc, 0, (name, out, err))

    def test_invalid_packages_rejected(self) -> None:
        cases: list[tuple[PkgKw, str]] = [
            ({"extra": {"task_id": "A-1"}}, "task_id"),
            ({"extra": {"task_id": "aa-01"}}, "task_id"),
            ({"argv": ["./missing.sh"]}, "entry"),
            ({"argv": ["../outside.sh"]}, "entry"),
            ({"env": {"CUDA_VISIBLE_DEVICES": "0"}}, "env"),
            ({"env": {"TASKMGR_X": "0"}}, "env"),
            ({"env": {"A-B": "0"}}, "env"),
            ({"env": {"1X": "0"}}, "env"),
            ({"env": {"": "0"}}, "env"),
            ({"gpus": [0, 0]}, "gpu"),
            ({"gpus": ["MIG-abc"]}, "gpu"),
            ({"extra": {"timeout_seconds": 0}}, "schema"),
        ]
        for kw, code in cases:
            pkg = self.make_pkg(**kw)
            rc, out, _ = self.task("validate", "package", pkg)
            self.assertEqual(rc, 1, kw)
            self.assertIn(code, [e["code"] for e in self.objs(out, "errors")], (kw, out))
        pkg = self.make_pkg()
        os.unlink(os.path.join(pkg, "TASK.md"))
        rc, out, _ = self.task("validate", "package", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("missing_file", [e["code"] for e in self.objs(out, "errors")])
        pkg = self.make_pkg()
        with open(os.path.join(pkg, "progress.json"), "w") as f:
            _ = f.write('{"schema_version": 1, "stage": "x"}')
        self.assertEqual(self.task("validate", "package", pkg)[0], 1)
        pkg = self.make_pkg()
        with open(os.path.join(pkg, "TASK.md"), "a") as f:
            _ = f.write("\n<REQUIRED: fill me>\n")
        self.assertEqual(self.task("validate", "package", pkg)[0], 1)

    def test_symlinks_and_special_files(self) -> None:
        pkg = self.make_pkg()
        os.symlink("/etc/passwd", os.path.join(pkg, "abs"))
        rc, out, _ = self.task("validate", "package", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("symlink", [e["code"] for e in self.objs(out, "errors")])
        pkg = self.make_pkg()
        os.makedirs(os.path.join(pkg, "d"))
        os.symlink("../../x", os.path.join(pkg, "d", "up"))
        self.assertEqual(self.task("validate", "package", pkg)[0], 1)
        pkg = self.make_pkg()
        os.mkfifo(os.path.join(pkg, "fifo"))
        rc, out, _ = self.task("validate", "package", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("special_file", [e["code"] for e in self.objs(out, "errors")])
        pkg = self.make_pkg(files={"d/f": "x"})
        os.symlink("d/f", os.path.join(pkg, "ok"))  # internal relative link is fine
        self.assertEqual(self.task("validate", "package", pkg)[0], 0)

    def test_command_name_entry_passes_validation(self) -> None:
        pkg = self.make_pkg(argv=["bash", "-c", "exit 0"])
        self.assertEqual(self.task("validate", "package", pkg)[0], 0)
        pkg = self.make_pkg(argv=["definitely-not-a-command-xyz"])
        self.assertEqual(self.task("validate", "package", pkg)[0], 0)  # checked remotely

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
        self.assertEqual(self.task("validate", "package", self._tar_with([]))[0], 0)
        for extra in bad:
            path = self._tar_with(extra)
            rc, out, _ = self.task("validate", "package", path)
            self.assertEqual(rc, 1, (extra, out))
            self.assertFalse(self.get(out, "valid"))
            # The remote side refuses it independently of the local check.
            with open(path, "rb") as f:
                rc, out, _ = self.remote("create", "job", stdin=f)
            self.assertEqual(rc, 2, (extra, out))
            self.assertEqual(self.get(out, "error", "code"), "unsafe_package")
        self.assertEqual(os.listdir(os.path.join(self.root(), "queue")), [])
        self.assertEqual(os.listdir(os.path.join(self.root(), "incoming")), [])

    def test_remote_rejects_oversized_package(self) -> None:
        self.set_config(max_package_bytes=1000)
        pkg = self.make_pkg(files={"big.bin": "x" * 5000})
        rc, out, _ = self.task("create", "job", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "package_too_large")

    def test_remote_rejects_unknown_gpu(self) -> None:
        pkg = self.make_pkg(gpus=[7])
        rc, out, _ = self.task("create", "job", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "invalid_gpus")
        pkg = self.make_pkg(gpus=["GPU-doesnotexist"])
        self.assertEqual(self.task("create", "job", "--host", "s1", pkg)[0], 2)
        # Index and UUID naming the same device are duplicates.
        pkg = self.make_pkg(gpus=[0, "GPU-00000000-0000-0000-0000-000000000000"])
        rc, out, _ = self.task("create", "job", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", self.text(out, "error", "message"))

    def test_wrapped_archive_with_top_level_directory(self) -> None:
        pkg = self.make_pkg()
        path = os.path.join(self.tmp, "wrapped.tar.gz")
        with tarfile.open(path, "w:gz") as tar:
            tar.add(pkg, arcname="AA-01")
        out = self.ok("create", "job", "--host", "s1", path)
        self.assertEqual(self.get(out, "task_id"), "AA-01")
        self.assertEqual(self.get(out, "content_hash"), package.content_hash(pkg))

    def test_symlink_chain_escaping_package_is_rejected(self) -> None:
        pkg = self.make_pkg()
        os.symlink(".", os.path.join(pkg, "x"))
        os.symlink("x/..", os.path.join(pkg, "y"))  # lexically ".", physically the parent
        rc, out, _ = self.task("validate", "package", pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(
            [e["path"] for e in self.objs(out, "errors") if e["code"] == "symlink"], ["y"]
        )
        path = self._tar_with(
            [
                ({"name": "x", "type": tarfile.SYMTYPE, "linkname": "."}, None),
                ({"name": "y", "type": tarfile.SYMTYPE, "linkname": "x/.."}, None),
            ]
        )
        self.assertEqual(self.task("validate", "package", path)[0], 1)
        with open(path, "rb") as f:
            rc, out, _ = self.remote("create", "job", stdin=f)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "unsafe_package"))

    def test_validate_archive_reports_all_errors(self) -> None:
        path = self._tar_with(
            [({"name": "../a", "mode": 0o644}, b"x"), ({"name": "/b", "mode": 0o644}, b"x")]
        )
        rc, out, _ = self.task("validate", "package", path)
        self.assertEqual(rc, 1)
        self.assertEqual(
            len([e for e in self.objs(out, "errors") if e["code"] == "unsafe_package"]), 2
        )
        junk = os.path.join(self.tmp, "junk.tar.gz")
        with open(junk, "w") as f:
            _ = f.write("not an archive")
        rc, out, _ = self.task("validate", "package", junk)
        self.assertEqual((rc, self.get(out, "errors", 0, "code")), (1, "bad_archive"))
        rc, out, _ = self.task("validate", "package", "--", "\n")
        self.assertEqual(rc, 2)

    def test_extract_never_writes_through_links(self) -> None:
        def extract(entries: list[TarEntry], skip: bool = False) -> tuple[str, list[str]]:
            n = len(os.listdir(self.tmp))
            path = os.path.join(self.tmp, "x-%d.tar.gz" % n)
            write_tar(path, [(dict(kw), data) for kw, data in entries])  # write_tar pops
            dest = os.path.join(self.tmp, "x-%d" % n, "d")
            skipped = package.safe_extract(path, dest, skip_unsafe=skip)
            return dest, [s["path"] for s in skipped]

        def outside(dest: str) -> list[str]:
            return sorted(set(os.listdir(os.path.dirname(dest))) - {"d"})

        link = tarfile.SYMTYPE
        # Chained links that pass the lexical check are refused when created.
        chain: list[TarEntry] = [
            ({"name": "x", "type": link, "linkname": "."}, None),
            ({"name": "y", "type": link, "linkname": "x/x/../../F"}, None),
            ({"name": "y", "mode": 0o644}, b"through the link"),
        ]
        with self.assertRaisesRegex(TaskmgrError, "resolves outside"):
            _ = extract(chain)
        dest, skipped = extract(chain, skip=True)
        self.assertEqual(skipped, ["y"])
        self.assertEqual(outside(dest), [])
        with open(os.path.join(dest, "y"), "rb") as f:  # the file, not a link
            self.assertEqual(f.read(), b"through the link")
        # A later link redirects an earlier one: y -> a/../F escapes once a -> . exists.
        dest, skipped = extract(
            [
                ({"name": "y", "type": link, "linkname": "a/../F"}, None),
                ({"name": "a", "type": link, "linkname": "."}, None),
            ],
            skip=True,
        )
        self.assertEqual(skipped, ["y"])
        self.assertFalse(os.path.lexists(os.path.join(dest, "y")))
        # Names differing only in case are one file on macOS: the second is a duplicate,
        # so "Y" is never opened through the link "y".
        dest, skipped = extract(
            [
                ({"name": "t", "mode": 0o644}, b"t"),
                ({"name": "y", "type": link, "linkname": "t"}, None),
                ({"name": "Y", "mode": 0o644}, b"evil"),
                ({"name": "Y", "type": tarfile.DIRTYPE, "mode": 0o755}, None),
            ],
            skip=True,
        )
        self.assertEqual(skipped, ["Y", "Y"])
        with open(os.path.join(dest, "t"), "rb") as f:
            self.assertEqual(f.read(), b"t")
        # A path that already exists (here a directory made for a/b) is never reused.
        dest, skipped = extract(
            [({"name": "a/b", "mode": 0o644}, b"b"), ({"name": "a", "mode": 0o644}, b"a")],
            skip=True,
        )
        self.assertEqual(skipped, ["a"])
        self.assertTrue(os.path.isfile(os.path.join(dest, "a", "b")))

    def test_pack_and_hash_skip_excluded_files(self) -> None:
        kept = {"run.py": "x", "uv.lock": "k", "sub/result.json": "r", "sub/m.py": "m"}
        junk = {
            "__pycache__/run.cpython-310.pyc": "j",
            "sub/__pycache__/m.pyc": "j",
            "sub/old.pyc": "j",
            ".git/HEAD": "j",
            "result.json": "j",
            "result.json.lock": "j",
            "progress.json.lock": "j",
            ".tmp-abc": "j",
            "sub/.DS_Store": "j",
        }
        pkg = self.make_pkg(files=kept)
        h = package.content_hash(pkg)
        for rel, content in junk.items():
            os.makedirs(os.path.join(pkg, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(pkg, rel), "w") as f:
                _ = f.write(content)
            self.assertTrue(package.is_excluded(rel), rel)
        os.symlink("/etc/passwd", os.path.join(pkg, ".git", "abs"))  # never packed
        self.assertEqual(package.content_hash(pkg), h)
        archive = os.path.join(self.tmp, "p.tgz")
        package.pack_dir(pkg, archive)
        with tarfile.open(archive) as t:
            names = {m.name for m in t.getmembers() if m.isfile()}
        self.assertEqual(names, set(kept) | {"task.json", "TASK.md", "progress.json"})
        self.assertEqual(self.task("validate", "package", pkg)[0], 0)
        self.assertEqual(self.task("validate", "package", archive)[0], 0)

    def test_validate_checks_size_and_entry_exec_bit(self) -> None:
        pkg = self.make_pkg(argv=["./bin/run.sh"], files={"bin/run.sh": "#!/bin/sh\n"})
        rc, out, _ = self.task("validate", "package", pkg)
        self.assertEqual(rc, 1)
        self.assertIn("not executable", self.text(out, "errors", 0, "message"))
        os.chmod(os.path.join(pkg, "bin", "run.sh"), 0o755)
        self.assertEqual(self.task("validate", "package", pkg)[0], 0)
        # An entry under an excluded path would not be packed.
        pkg = self.make_pkg(argv=["./.git/run.sh"], files={".git/run.sh": "#!/bin/sh\n"})
        os.chmod(os.path.join(pkg, ".git", "run.sh"), 0o755)
        rc, out, _ = self.task("validate", "package", pkg)
        self.assertIn("not a file in the package", self.text(out, "errors", 0, "message"))
        pkg = self.make_pkg(files={"big.bin": "x" * 5000, "__pycache__/c.pyc": "y" * 5000})
        self.assertEqual(package.validate_dir(pkg, max_bytes=None)[0], [])
        self.assertEqual(package.validate_dir(pkg)[0], [])
        errors, _ = package.validate_dir(pkg, max_bytes=6000)  # excluded files do not count
        self.assertEqual(errors, [])
        errors, _ = package.validate_dir(pkg, max_bytes=4000)
        self.assertEqual([e["code"] for e in errors], ["package_too_large"])

    def test_regexes_reject_trailing_newline(self) -> None:
        from taskmgr.common import SUBMISSION_KEY_RE, TASK_ID_RE

        self.assertIsNone(TASK_ID_RE.match("AA-01\n"))
        self.assertIsNone(SUBMISSION_KEY_RE.match("k\n"))


class ExcludedEntriesAndSizeTest(Env):
    """Review P1-1, P1-2, P1-3: excluded entries in archives, the root exclusion rule, and
    who decides the package size limit."""

    def _archive(self, pkg: str, extra: dict[str, str]) -> str:
        """An archive of pkg plus extra files (archive path -> content) that pack_dir
        would leave out."""
        path = os.path.join(self.tmp, "with-extra-%d.tar.gz" % len(os.listdir(self.tmp)))
        with tarfile.open(path, "w:gz") as tar:
            for name in sorted(os.listdir(pkg)):
                tar.add(os.path.join(pkg, name), arcname=name)
            for name, content in extra.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(content.encode())
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(content.encode()))
        return path

    def test_archived_result_json_is_removed_before_hash_and_run(self) -> None:
        pkg = self.make_pkg(script="true", required=["best"])
        stale = json.dumps({"schema_version": 1, "outputs": {"best": "stale"}})
        extra = {"result.json": stale, "result.json.lock": "", ".git/HEAD": "ref"}
        archive = self._archive(pkg, extra)
        rc, out, err = self.task("validate", "package", archive)
        self.assertEqual(rc, 0, (out, err))
        warned = {self.text(w, "path") for w in self.objs(out, "warnings")}
        self.assertEqual(warned, {"result.json", "result.json.lock", ".git"})
        self.assertIn("result.json", err)  # also printed on stderr
        sub = self.ok("create", "job", "--host", "s1", archive)
        self.assertEqual(self.get(sub, "content_hash"), package.content_hash(pkg))
        jobid = self.text(sub, "jobid")
        pkg_copy = os.path.join(self.job_dir(jobid), "package")
        self.assertEqual(sorted(os.listdir(pkg_copy)), ["TASK.md", "progress.json", "task.json"])
        _ = self.start_daemon()
        _ = self.wait_state(jobid, ("succeeded", "invalid_result", "failed"))
        self.assertEqual(self.job(jobid)["state"], "invalid_result")

    def test_root_exclusion_matches_exact_names(self) -> None:
        kept = ["result.jsonl", "result.json.bak", "result.json.d/x", "sub/result.json"]
        dropped = [
            "result.json",
            "result.json.lock",
            "sub/x.json.lock",
            "result.json.corrupt",
            "result.json.corrupt.2",
        ]
        for rel in kept:
            self.assertFalse(package.is_excluded(rel), rel)
        for rel in dropped + ["progress.json.lock"]:
            self.assertTrue(package.is_excluded(rel), rel)
        pkg = self.make_pkg(
            script="cat result.jsonl result.json.bak result.json.d/x > out.txt",
            files={k: k for k in kept + dropped},
        )
        rc, out, err = self.task("validate", "package", pkg)
        self.assertEqual(rc, 0, (out, err))
        warned = {self.text(w, "path") for w in self.objs(out, "warnings")}
        self.assertEqual(warned, set(dropped))
        archive = os.path.join(self.tmp, "p.tgz")
        package.pack_dir(pkg, archive)
        with tarfile.open(archive) as t:
            names = {m.name for m in t.getmembers() if m.isfile()}
        self.assertTrue(set(kept) <= names, names)
        jobid = self.submit(pkg)
        _ = self.start_daemon()
        _ = self.wait_state(jobid, ("succeeded", "invalid_result", "failed"))
        # The entry read the three files (it writes no result.json, hence invalid_result).
        meta = self.obj(self.result(jobid), "_taskmgr")
        self.assertEqual(meta["return_code"], 0, meta)

    def _main(self, *argv: str) -> tuple[int, JsonValue, str]:
        """Run the CLI in this process (to patch the local size default)."""
        import contextlib
        from unittest import mock

        from taskmgr import taskmgr

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = taskmgr.main([*argv[:2], "--plain", "-o", "json", *argv[2:]])
        text = out.getvalue()
        return rc, cast(JsonValue, json.loads(text)) if text.strip() else None, err.getvalue()

    def test_server_decides_the_package_size(self) -> None:
        from unittest import mock

        pkg = self.make_pkg(files={"big.bin": "x" * 5000})
        # Pretend the default limit is 1000 bytes: the local checks must not use it to
        # refuse a submission; the server's max_package_bytes decides.
        with mock.patch.object(package, "DEFAULT_MAX_PACKAGE_BYTES", 1000):
            self.set_config(max_package_bytes=None)
            rc, out, err = self._main("create", "job", "--host", "s1", pkg)
            self.assertEqual(rc, 0, (out, err))
            rc, out, err = self._main("validate", "package", pkg)
            self.assertEqual(rc, 0, (out, err))
            self.assertEqual(
                [self.text(w, "code") for w in self.objs(out, "warnings")], ["package_too_large"]
            )
            self.assertIn(
                "exceeds max_package_bytes (1000)", self.text(out, "warnings", 0, "message")
            )
        self.set_config(max_package_bytes=1000)
        rc, out, _ = self.task("create", "job", "--host", "s1", pkg)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(out, "error", "code"), "package_too_large")
        self.assertIn("exceeds max_package_bytes (1000)", self.text(out, "error", "message"))
