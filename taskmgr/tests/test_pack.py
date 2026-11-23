"""`task build package`: a task package from project files and options, path on stdout."""

import json
import os
import stat
import subprocess
import sys
import tarfile
from typing import cast

from taskmgr.records import JsonDict, JsonValue
from taskmgr.tests.helpers import REPO, TASK, Env

TEMPLATE = os.path.join(REPO, "taskmgr", "template")


class PackTest(Env):
    def project(self) -> str:
        """A project tree as a wrapper script would find it."""
        p = os.path.join(self.tmp, "proj")
        files = {
            "run.py": "import taskmgr_task as tt\ntt.write_result({'speedup': 1.5})\n",
            "spec/a.yaml": "a: 1\n",
            "spec/cache/big.bin": "x",
            "tools/gpu_exec.py": "print('tool')\n",
            "kernels/moe_kernel.py": "K = 1\n",
        }
        for rel, text in files.items():
            path = os.path.join(p, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                _ = f.write(text)
        return p

    def pack(
        self, *args: object, env: dict[str, str] | None = None, cwd: str | None = None
    ) -> tuple[int, str, str]:
        """Run `task build package` as a script would: text output, no -o json."""
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run(
            [sys.executable, TASK, "build", "package", "--plain"] + [str(a) for a in args],
            env=e,
            cwd=cwd or self.tmp,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def read_json(self, path: str) -> JsonDict:
        with open(path) as f:
            return self.obj(cast(JsonValue, json.load(f)))

    def read(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def test_pack_a_project_and_submit_it(self) -> None:
        p = self.project()
        out_dir = os.path.join(self.tmp, "out", "MO-01")
        rc, stdout, err = self.pack(
            os.path.join(p, "run.py"),
            os.path.join(p, "spec") + "=spec",
            os.path.join(p, "tools"),
            os.path.join(p, "kernels", "moe_kernel.py") + "=kernels.py",
            "--task-id",
            "MO-01",
            "--entry",
            "python3 run.py --cfg 'spec/a.yaml'",
            "--gpus",
            "none",
            "--env",
            "A=1",
            "--env",
            "B=x=y",
            "--output-key",
            "speedup",
            "--timeout",
            "600",
            "--require-path",
            "/",
            "--exclude",
            "cache",
            "--dest",
            out_dir,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(stdout, out_dir + "\n")  # only the path: "$(task build package ...)" works
        self.assertEqual(
            sorted(
                os.path.relpath(os.path.join(d, n), out_dir)
                for d, _, names in os.walk(out_dir)
                for n in names
            ),
            [
                "TASK.md",
                "gpu_exec.py",
                "kernels.py",
                "progress.json",
                "run.py",
                os.path.join("spec", "a.yaml"),
                "task.json",
                "taskmgr_task.py",
            ],
        )
        self.assertEqual(
            self.read_json(os.path.join(out_dir, "task.json")),
            {
                "schema_version": 1,
                "task_id": "MO-01",
                "entry": {"argv": ["python3", "run.py", "--cfg", "spec/a.yaml"]},
                "gpus": [],
                "environment": {"required_paths": ["/"]},
                "env": {"A": "1", "B": "x=y"},
                "timeout_seconds": 600,
                "result": {"required_outputs": ["speedup"]},
            },
        )
        self.assertEqual(
            self.read(os.path.join(out_dir, "taskmgr_task.py")),
            self.read(os.path.join(TEMPLATE, "taskmgr_task.py")),
        )
        self.assertIn(
            "python3 run.py --cfg spec/a.yaml", self.read(os.path.join(out_dir, "TASK.md")).decode()
        )
        _ = self.ok("validate", "package", out_dir)
        # The package runs.
        _ = self.start_daemon()
        j = self.submit(out_dir.strip())
        _ = self.wait_state(j, "succeeded")
        # The same again: the target exists, nothing is written without --force.
        rc, stdout, err = self.pack(p, "--task-id", "MO-01", "--entry", "true", "--dest", out_dir)
        self.assertEqual((rc, stdout), (2, ""))
        self.assertIn("already exists", err)
        rc, _, err = self.pack(
            p, "--task-id", "MO-01", "--entry", "true", "--dest", out_dir, "--force"
        )
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "tools", "gpu_exec.py")))

    def test_archive_output_is_deterministic(self) -> None:
        p = self.project()
        tar = os.path.join(self.tmp, "MO-02.tar.gz")
        args: list[object] = [
            p,
            "--task-id",
            "MO-02",
            "--entry",
            "python3 run.py",
            "--gpus",
            '{"count": 1, "min_free_gb": 40}',
            "--precompile",
            "python3 precompile.py kernels/moe_kernel.py",
            "--precompile-timeout",
            "900",
            "--with-cute-helpers",
            "--dest",
            tar,
        ]
        rc, stdout, err = self.pack(*args)
        self.assertEqual(rc, 0, err)
        self.assertEqual(stdout, tar + "\n")
        first = self.read(tar)
        rc, _, err = self.pack(*args, "--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read(tar), first)
        v = self.ok("validate", "package", tar)
        self.assertEqual(self.strs(v, "features"), ["gpu_select", "precompile"])
        detail = self.ok("build", "package", *args, "--force")
        self.assertEqual(self.get(detail, "content_hash"), self.get(v, "content_hash"))
        self.assertIs(self.get(detail, "archive"), True)
        self.assertEqual(self.strs(detail, "features"), ["gpu_select", "precompile"])
        with tarfile.open(tar) as t:
            names = t.getnames()
            member = t.extractfile("task.json")
            assert member is not None
            task = self.obj(cast(JsonValue, json.loads(member.read())))
        for name in ("precompile.py", "cutecache.py", "taskmgr_task.py", "spec/cache/big.bin"):
            self.assertIn(name, names)
        self.assertEqual(task["gpus"], {"count": 1, "min_free_gb": 40})
        self.assertEqual(
            task["precompile"],
            {"argv": ["python3", "precompile.py", "kernels/moe_kernel.py"], "timeout_seconds": 900},
        )

    def test_task_json_from_the_sources_is_the_base(self) -> None:
        p = self.project()
        with open(os.path.join(p, "task.json"), "w") as f:
            json.dump(
                {
                    "schema_version": 1,
                    "task_id": "BA-01",
                    "entry": {"argv": ["python3", "run.py"]},
                    "gpus": [0],
                    "env": {"A": "old", "KEEP": "1"},
                    "result": {"required_outputs": ["a"]},
                    "x-owner": "me",
                },
                f,
            )
        with open(os.path.join(p, "taskmgr_task.py"), "w") as f:
            _ = f.write("# an old copy\n")
        os.symlink(os.path.join(self.tmp, "outside.txt"), os.path.join(p, "link.txt"))
        with open(os.path.join(self.tmp, "outside.txt"), "w") as f:
            _ = f.write("followed\n")
        rc, stdout, err = self.pack(p, "--env", "A=new", "--output-key", "b", "--timeout", "60")
        self.assertEqual(rc, 0, err)
        out = stdout.strip()
        self.assertEqual(out, os.path.join(self.tmp, "BA-01"))  # default ./<task_id>
        task = self.read_json(os.path.join(out, "task.json"))
        self.assertEqual(task["gpus"], [0])
        self.assertEqual(task["env"], {"A": "new", "KEEP": "1"})
        self.assertEqual(task["result"], {"required_outputs": ["a", "b"]})
        self.assertEqual((task["timeout_seconds"], task["x-owner"]), (60, "me"))
        self.assertIn("taskmgr_task.py from the sources was replaced", err)
        self.assertEqual(
            self.read(os.path.join(out, "taskmgr_task.py")),
            self.read(os.path.join(TEMPLATE, "taskmgr_task.py")),
        )
        link = os.path.join(out, "link.txt")
        self.assertTrue(stat.S_ISREG(os.lstat(link).st_mode))
        self.assertEqual(self.read(link), b"followed\n")

    def tree(self, root: str) -> list[str]:
        """Every file under root, relative, sorted."""
        return sorted(
            os.path.relpath(os.path.join(d, n), root)
            for d, _, names in os.walk(root)
            for n in names
        )

    def test_force_never_deletes_the_cwd_or_a_source(self) -> None:
        p = os.path.join(self.tmp, "AB-02")  # named like the task: the default --dest
        os.rename(self.project(), p)
        with open(os.path.join(p, "notes.txt"), "w") as f:
            _ = f.write("not packed\n")
        before = self.tree(p)
        entry = ("--task-id", "AB-02", "--entry", "python3 run.py", "--force")
        for args, cwd, what in (
            # --dest is the directory that holds the source file.
            ([os.path.join(p, "run.py"), *entry, "--dest", p], self.tmp, "contains source"),
            # The default ./<task_id> is the source directory.
            (["AB-02", *entry], self.tmp, "is source AB-02"),
            # --dest is the current directory.
            ([".", *entry, "--dest", "."], p, "is the current directory"),
            # --dest holds the current directory.
            (
                ["../run.py", *entry, "--dest", ".."],
                os.path.join(p, "spec"),
                "contains the current",
            ),
        ):
            with self.subTest(args=args):
                rc, stdout, err = self.pack(*args, cwd=cwd)
                self.assertEqual((rc, stdout), (2, ""), err)
                self.assertIn("(bad_args)", err)
                self.assertIn(what, err)
                self.assertIn("nothing was deleted", err)
                self.assertEqual(self.tree(p), before)

    def test_force_replaces_only_a_package(self) -> None:
        p = self.project()
        args = (p, "--task-id", "AB-03", "--entry", "python3 run.py", "--force")
        # A directory that is not a package (no task.json) and a file that is no archive.
        other = os.path.join(self.tmp, "data")
        os.makedirs(other)
        with open(os.path.join(other, "keep.txt"), "w") as f:
            _ = f.write("keep\n")
        plain = os.path.join(self.tmp, "notes.txt")
        with open(plain, "w") as f:
            _ = f.write("keep\n")
        for dest, what in ((other, "without a task.json"), (plain, "not named .tar.gz")):
            with self.subTest(dest=dest):
                rc, stdout, err = self.pack(*args, "--dest", dest)
                self.assertEqual((rc, stdout), (2, ""), err)
                self.assertIn(what, err)
                self.assertIn("nothing was deleted", err)
        self.assertEqual(self.tree(other), ["keep.txt"])
        self.assertTrue(os.path.isfile(plain))

        # An earlier package, an archive and a symlink are replaced.
        pkg = os.path.join(self.tmp, "AB-03")
        tgz = os.path.join(self.tmp, "AB-03.tgz")
        link = os.path.join(self.tmp, "link")
        os.symlink(other, link)
        for dest in (pkg, pkg, tgz, tgz, link):
            with self.subTest(dest=dest):
                rc, stdout, err = self.pack(*args, "--dest", dest)
                self.assertEqual((rc, stdout), (0, dest + "\n"), err)
        self.assertTrue(os.path.isfile(os.path.join(link, "task.json")))
        self.assertFalse(os.path.islink(link))
        self.assertEqual(self.tree(other), ["keep.txt"])

    def test_errors(self) -> None:
        p = self.project()
        out = os.path.join(self.tmp, "X")
        for args, code in (
            ([p, "--entry", "true", "--dest", out], "--task-id is required"),
            ([p, "--task-id", "AA-01", "--dest", out], "--entry is required"),
            ([p, "--task-id", "bad", "--entry", "true", "--dest", out], "task_id must match"),
            ([os.path.join(self.tmp, "nope"), "--task-id", "AA-01"], "does not exist"),
            ([p, p + "/run.py", "--task-id", "AA-01", "--entry", "true"], "written by two sources"),
            ([p + "=../up", "--task-id", "AA-01", "--entry", "true"], "outside the package"),
            ([p, "--task-id", "AA-01", "--entry", "true", "--env", "NOEQ"], "NAME=VALUE"),
            ([p, "--task-id", "AA-01", "--entry", "true", "--gpus", "{bad"], "not valid JSON"),
        ):
            with self.subTest(args=args):
                rc, stdout, err = self.pack(*args)
                self.assertEqual((rc, stdout), (2, ""), err)
                self.assertIn(code, err)
        # An invalid package: exit 1, errors on stderr only, nothing written.
        rc, stdout, err = self.pack(
            p, "--task-id", "AA-01", "--entry", "./run.py", "--gpus", '{"count": 0}', "--dest", out
        )
        self.assertEqual((rc, stdout), (1, ""))
        self.assertIn("not executable", err)
        self.assertIn("gpus.count", err)
        self.assertFalse(os.path.exists(out))
        detail = self.task(
            "build", "package", p, "--task-id", "AA-01", "--entry", "./run.py", "--dest", out
        )
        self.assertEqual(detail[0], 1)
        self.assertIs(self.get(detail[1], "valid"), False)
        self.assertEqual(self.get(detail[1], "errors", 0, "code"), "entry")
