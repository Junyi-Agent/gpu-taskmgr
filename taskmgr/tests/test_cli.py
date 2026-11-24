"""The kubectl-style command line (verbs, singular and plural resources, the old forms
rejected), text output (the default) and -o json, the grouped help, jobid lookup without
--host, get user and get version."""

import argparse
import getpass
import json
import os
import re
import socket
import unittest
from collections.abc import Mapping
from typing import cast

from taskmgr import remote
from taskmgr.tests.helpers import OK_RESULT, Env

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class CliTest(Env):
    servers: tuple[str, ...] = ("s1", "s2")

    def plain(self, *args: object) -> tuple[int, str, str]:
        rc, out, err = self.task(*args, text=True)
        return rc, out if isinstance(out, str) else "", err

    def colored(self, *args: object) -> tuple[int, str, str]:
        rc, out, err = self.task(*args, text=True, color=True)
        return rc, out if isinstance(out, str) else "", err

    def top_commands(self) -> list[list[str]]:
        """The commands `task -h` lists, as [verb] or [verb, resource]."""
        _, top, _ = self.plain("-h")
        cmds: list[list[str]] = []
        for line in top.split("\noptions\n")[0].splitlines():
            if line.startswith("  ") and not line.startswith("   "):
                words = line[2:].split("  ")[0].split()
                cmds.append([w for w in words[:2] if w.isalpha() and w.islower()])
        return cmds

    def test_help_lists_commands_by_group(self) -> None:
        rc, out, _ = self.plain("-h")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("usage: task <command> <resource> [args]\n"), out)
        heads = [line for line in out.splitlines() if re.fullmatch("[a-z]+", line)]
        self.assertEqual(heads, ["jobs", "packages", "hosts", "others", "options"])
        names = [" ".join(c) for c in self.top_commands()]
        self.assertEqual(
            names,
            [
                "create job",
                "get jobs",
                "wait job",
                "logs job",
                "fetch job",
                "cancel job",
                "retry job",
                "resolve job",
                "create package",
                "build package",
                "validate package",
                "create host",
                "get hosts",
                "update host",
                "delete host",
                "enable host",
                "disable host",
                "ping host",
                "build pyz",
                "get user",
                "get version",
                "update cli",
            ],
        )
        self.assertRegex(out, r"(?m)^  -o, --output text\|json +output format")

    def test_every_help_has_the_same_layout(self) -> None:
        """`task -h`, `task <verb> -h` and every `task <verb> <resource> -h`, the resource
        singular or plural: usage line, description sentence, bold sections of (cyan name,
        help) entries, -h last, a footer naming the next help."""
        plural = {"job": "jobs", "host": "hosts", "package": "packages"}
        cmds = self.top_commands()
        verbs: list[list[str]] = [[v] for v in sorted({c[0] for c in cmds})]
        singulars = [[c[0], c[1].rstrip("s")] for c in cmds if len(c) == 2]
        plurals = [[v, plural[r]] for v, r in singulars if r in plural]
        for cmd in [[], *verbs, *singulars, *plurals]:
            rc, out, err = self.plain(*cmd, "-h")
            self.assertEqual((rc, err), (0, ""), cmd)
            blocks = out.rstrip("\n").split("\n\n")
            shown = cmd[:1] + [r.rstrip("s") for r in cmd[1:]]  # the singular name
            self.assertTrue(blocks[0].startswith(" ".join(["usage: task", *shown])), cmd)
            self.assertRegex(blocks[1], r"^[A-Z][\s\S]*\.$", cmd)  # description sentence
            self.assertRegex(blocks[-1], r"^`task [^`]*-h` [^\n]*\.$", cmd)  # footer
            titles = [sec.split("\n")[0] for sec in blocks[2:-1]]
            if cmd:
                order = ["resources", "arguments", "options"]
                self.assertEqual(titles, [t for t in order if t in titles], cmd)
                self.assertEqual("resources" in titles, len(cmd) == 1, cmd)
            for sec in blocks[2:-1]:
                for e in sec.split("\n")[1:]:
                    # "  name  help" (or a wrapped continuation line): every entry has help
                    self.assertRegex(e, r"^  (\S.*?  +| +)\S", (cmd, e))
            last = blocks[-2].split("\n")[-1]
            self.assertRegex(last, r"^  -h, --help +show this help$", cmd)
            # Colored: bold usage and titles, cyan names; without colors the same text.
            _, colored, _ = self.colored(*cmd, "-h")
            self.assertEqual(ANSI.sub("", colored), out, cmd)
            self.assertTrue(colored.startswith("\x1b[1musage:\x1b[0m "), cmd)
            self.assertIn("\x1b[1moptions\x1b[0m\n  \x1b[36m", colored, cmd)
        _, out, _ = self.plain("get", "-h")
        self.assertRegex(out, r"(?m)^  job \(jobs\) +list the jobs")
        self.assertRegex(out, r"(?m)^  host \(hosts\) +list the configured servers")

    def test_usage_errors_point_to_the_help(self) -> None:
        cases: list[tuple[list[str], str, str]] = [
            ([], "the following arguments are required: <command>", "`task -h` lists all commands"),
            (["bogus"], "unknown command 'bogus'", "`task -h` lists all commands"),
            (["get"], "required: <resource>", "`task get -h` lists its resources"),
            (
                ["get", "pods"],
                "unknown resource 'pods' for `task get` (choose from job, jobs, host, hosts, user, version)",
                "`task get -h` lists its resources",
            ),
            (
                ["cancel", "0123456789abcdef"],
                "unknown resource '0123456789abcdef' for `task cancel` (choose from job, jobs)",
                "`task cancel -h` lists its resources",
            ),
            (
                ["retry", "job"],
                "required: ID",
                "`task retry job -h` shows its arguments and options",
            ),
            (
                ["delete", "hosts"],
                "required: name",
                "`task delete host -h` shows its arguments and options",
            ),
            (
                ["build", "package", "-o", "out/"],
                "-o/--output is the output format, text or json, not 'out/' (a path is given "
                + "with --dest)",
                "`task build package -h` shows its arguments and options",
            ),
            (
                ["get", "jobs", "--detail"],
                "unrecognized arguments: --detail (--detail is now -o json)",
                "`task -h` lists all commands",
            ),
        ]
        for argv, error, hint in cases:
            rc, out, err = self.plain(*argv)
            self.assertEqual((rc, out), (2, ""), argv)
            err_line, hint_line = err.splitlines()[-2:]
            self.assertTrue(err.startswith("usage: task"), argv)
            self.assertIn(error, err_line, argv)
            self.assertTrue(err_line.startswith("error: "), argv)
            self.assertEqual(hint_line, "hint: " + hint, argv)

    def test_old_commands_are_rejected_with_the_new_form(self) -> None:
        cases = [
            (["submit", "pkg/"], "`task submit` is now `task create job PKG`"),
            (["jobs"], "`task jobs` is now `task get jobs`"),
            (
                ["status", "0123456789abcdef", "--wait"],
                "`task status` is now `task get job ID` (with --wait: `task wait job ID`)",
            ),
            (["init", "AA-01"], "`task init` is now `task create package TASK_ID`"),
            (["pack", "src/"], "`task pack` is now `task build package SRC...`"),
            (["host", "list"], "`task host` is now `task <create|get|update|delete|enable"),
            (["whoami"], "`task whoami` is now `task get user`"),
            (["version"], "`task version` is now `task get version`"),
            (["update"], "`task update` alone is now `task update cli`"),
        ]
        for argv, error in cases:
            rc, out, err = self.plain(*argv)
            self.assertEqual((rc, out), (2, ""), argv)
            self.assertIn("error: " + error, err, argv)
            # With -o json, a usage error is a JSON object on stdout too.
            rc, obj, _ = self.task(*argv, "-o", "json", text=True)
            self.assertEqual((rc, self.get(obj, "error", "code")), (2, "bad_args"), argv)
        # The old -o PATH of fetch / create package is --dest now.
        rc, _, err = self.plain("fetch", "job", "0123456789abcdef", "--output", "r/")
        self.assertEqual(rc, 2)
        self.assertIn("a path is given with --dest", err)

    def test_singular_and_plural_resource_names(self) -> None:
        j = self.submit(self.make_pkg(task_id="EE-05"), host="s1")
        for res in ("job", "jobs"):
            self.assertEqual(self.objs(self.ok("get", res), "jobs")[0]["jobid"], j, res)
            self.assertEqual(self.get(self.ok("get", res, j), "jobid"), j, res)
        for res in ("host", "hosts"):
            rows = self.objs(self.ok("get", res), "servers")
            self.assertEqual([r["name"] for r in rows], ["s1", "s2"], res)
            rows = self.objs(self.ok("get", res, "s2"), "servers")
            self.assertEqual([r["name"] for r in rows], ["s2"], res)
            self.assertEqual(self.get(self.ok("ping", res, "s1"), "complete"), True, res)
        for res in ("package", "packages"):
            out = self.ok("validate", res, self.make_pkg(task_id="EE-06"))
            self.assertEqual(self.get(out, "task_id"), "EE-06", res)
        rc, obj, _ = self.task("get", "hosts", "nope")
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "unknown_host"))
        # --state and -n only apply to the list.
        rc, obj, err = self.task("get", "job", j, "--state", "queued")
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "bad_args"), err)
        rc, obj, err = self.task("get", "job", j, "-n", "3")
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "bad_args"), err)

    def test_output_text_and_json(self) -> None:
        """-o text is the default; -o json, --output json, --output=json and -ojson print the
        JSON object; anything else is a usage error."""
        pkg = self.make_pkg(task_id="FF-07")
        text = self.plain("validate", "package", pkg)[1]
        self.assertRegex(text, r"^valid: FF-07, content_hash \S+\n$")
        self.assertEqual(self.plain("validate", "package", pkg, "-o", "text")[1], text)
        obj = self.ok("validate", "package", pkg)
        for flag in (["-o", "json"], ["--output", "json"], ["--output=json"], ["-ojson"]):
            rc, got, err = self.task("validate", "package", pkg, *flag, text=True)
            self.assertEqual((rc, got, err), (0, obj, ""), flag)
        self.assertEqual(self.get(obj, "valid"), True)
        rc, _, err = self.plain("validate", "package", pkg, "-o", "yaml")
        self.assertEqual(rc, 2)
        self.assertIn("not 'yaml'", err)
        # The same JSON object and text for the server reports and host commands.
        version = self.ok("get", "version")
        self.assertEqual(
            sorted(self.obj(version)), ["complete", "errors", "hosts", "local", "skipped"]
        )
        rc, out, _ = self.plain("get", "hosts", "-o", "text")
        self.assertEqual(
            (rc, out.split()[:4]), (0, ["NAME", "ENABLED", "DESTINATION", "REMOTE_ROOT"])
        )
        # logs: the log is raw text with -o json too; an error is a JSON object on stdout.
        rc, obj, err = self.task("logs", "job", "0123456789abcdef", "-o", "json")
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "not_found"), err)

    def test_usage_names_the_primary_option(self) -> None:
        """The usage line shows an option's first name, as the docs do (--plan, not the
        longer alias --dry-run); the option list shows every name."""
        _, out, _ = self.plain("create", "host", "-h")
        usage = out.split("\n\n")[0]
        for word in ("[--yes]", "[--plan]", "[-o text|json]"):
            self.assertIn(word, usage)
        self.assertNotIn("--dry-run", usage)
        self.assertNotIn("--output", usage)
        self.assertRegex(out, r"(?m)^  --plan, --dry-run +check everything")
        self.assertRegex(out, r"(?m)^  -y, --yes +answer yes")
        # build package names both archive suffixes.
        _, out, _ = self.plain("build", "package", "-h")
        self.assertIn(".tgz", out.split("\n\n")[1])
        self.assertIn("a file ending in .tar.gz or .tgz", " ".join(out.split()))

    def test_removed_attempt_option_is_a_usage_error(self) -> None:
        rc, out, err = self.plain("logs", "job", "0123456789abcdef", "--attempt", "1")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("--attempt", err)

    def test_validate_and_init_text(self) -> None:
        target = os.path.join(self.tmp, "AA-01")
        rc, out, _ = self.plain("create", "package", "AA-01", "--dest", target)
        self.assertEqual(rc, 0)
        self.assertIn("created: AA-01 in %s\nhint: fill every" % target, out)
        rc, out, err = self.plain("validate", "package", target)
        self.assertEqual((rc, err), (1, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^invalid: \d+ errors$")
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertIn("placeholder", out)
        rc, out, _ = self.plain("validate", "package", self.make_pkg(task_id="BB-02"))
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^valid: BB-02, content_hash \S+\n$")
        rc, colored, _ = self.colored("validate", "package", target)
        self.assertIn("\x1b[31minvalid", colored)
        # A missing path: one error line on stderr, nothing on stdout.
        rc, out, err = self.plain("validate", "package", os.path.join(self.tmp, "nope"))
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(
            err, "error: path does not exist: %s (not_found)\n" % os.path.join(self.tmp, "nope")
        )
        # -o json: the JSON object (on errors too).
        rc, obj, _ = self.task(
            "validate", "package", "-o", "json", os.path.join(self.tmp, "nope"), text=True
        )
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "not_found"))

    def test_job_commands_find_the_host_by_jobid(self) -> None:
        # The job waits for `gate`: the first run finds it, the re-run (after the gate is
        # removed) waits, so it is still queued or running when it is cancelled.
        gate = os.path.join(self.tmp, "gate")
        open(gate, "w").close()
        wait = "while [ ! -f %s ]; do sleep 0.05; done; " % gate
        rc, out, _ = self.plain(
            "create", "job", "--host", "s2", self.make_pkg(script=wait + "echo hi; " + OK_RESULT)
        )
        self.assertEqual(rc, 0)
        header, row = out.splitlines()
        self.assertEqual(header.split(), ["JOBID", "TASK_ID", "HOST", "STATE"])
        j = row.split()[0]
        _ = self.start_daemon("s2")
        _ = self.wait_state(j, "succeeded", name="s2")
        rc, out, _ = self.plain("get", "job", j)
        self.assertEqual(rc, 0)
        fields = dict(
            (k.strip(), v.strip()) for k, v in (line.split(":", 1) for line in out.splitlines())
        )
        self.assertEqual(
            (fields["jobid"], fields["host"], fields["state"], fields["return_code"]),
            (j, "s2", "succeeded", "0"),
        )
        self.assertEqual(self.plain("logs", "job", j), (0, "hi\n", ""))
        rc, out, _ = self.plain("fetch", "job", j, "--dest", os.path.join(self.tmp, "r"))
        self.assertEqual(rc, 0)
        self.assertIn("fetched: job %s from s2 into " % j, out)
        os.unlink(gate)
        rc, out, _ = self.plain("retry", "job", j)
        self.assertEqual(rc, 0)
        new = out.splitlines()[1].split()[0]
        rc, out, _ = self.plain("cancel", "job", new)
        self.assertEqual(rc, 0)
        self.assertIn("cancel requested: job %s on s2" % new, out)
        rc, out, _ = self.plain("resolve", "job", new)  # not unknown: nothing to do
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^host: +s2$")
        self.assertRegex(out, r"(?m)^changed: +no$")
        # The JSON objects still carry the host.
        self.assertEqual(self.get(self.ok("get", "job", j), "host"), "s2")
        # A jobid nobody has: an error naming it.
        rc, out, err = self.plain("cancel", "job", "0123456789abcdef")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("job 0123456789abcdef not found on any server", err)

    def test_status_text_of_a_queued_job(self) -> None:
        j = self.submit(self.make_pkg(task_id="CC-03"), host="s1")  # no daemon: stays queued
        rc, out, _ = self.colored("get", "job", j)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[33mqueued\x1b[0m", out)
        plain = ANSI.sub("", out)
        self.assertRegex(plain, r"(?m)^task_id: +CC-03$")
        self.assertRegex(plain, r"(?m)^host: +s1$")

    def test_colors_everywhere_unless_plain(self) -> None:
        pkg = self.make_pkg(task_id="DD-04")
        # Colored on a pipe: text, JSON, errors on stderr, help, usage errors.
        rc, out, _ = self.colored("validate", "package", pkg)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[32mvalid:\x1b[0m", out)
        rc, out, _ = self.colored("validate", "package", pkg, "-o", "json")
        self.assertIn('\x1b[34m"valid"\x1b[0m: \x1b[35mtrue\x1b[0m', out)
        self.assertEqual(json.loads(ANSI.sub("", out)), self.ok("validate", "package", pkg))
        rc, out, err = self.colored("validate", "package", os.path.join(self.tmp, "nope"))
        self.assertEqual((rc, out), (2, ""))
        self.assertTrue(err.startswith("\x1b[31merror:\x1b[0m "), err)
        rc, out, _ = self.colored("-h")
        self.assertIn("\x1b[1mjobs\x1b[0m", out)
        rc, out, err = self.colored("retry")
        self.assertEqual(rc, 2)
        self.assertIn("\x1b[31merror:\x1b[0m the following arguments are required", err)
        # --plain anywhere on the line, or NO_COLOR, turns every color off.
        for argv in (
            ["--plain", "validate", "package", pkg, "-o", "json"],
            ["validate", "--plain", "package", pkg, "-o", "json"],
            ["validate", "package", "--plain", pkg, "-o", "json"],
            ["validate", "package", pkg, "-o", "json", "--plain"],
        ):
            rc, obj, err = self.task(*argv, text=True, color=True)  # stdout parsed as JSON
            self.assertEqual((rc, err), (0, ""), argv)
            self.assertEqual(obj, self.ok("validate", "package", pkg), argv)
        for argv in (
            ["-h", "--plain"],
            ["retry", "--plain"],
            ["retry", "job", "--plain"],
            ["validate", "package", "--plain", "nope"],
        ):
            _, out, err = self.colored(*argv)
            self.assertNotIn("\x1b[", out + err, argv)
        rc, out, err = self.task(
            "validate", "package", pkg, text=True, color=True, env={"NO_COLOR": "1"}
        )
        self.assertEqual((rc, out), (0, ANSI.sub("", self.colored("validate", "package", pkg)[1])))

    def test_get_user_and_version(self) -> None:
        rc, out, _ = self.plain("get", "user")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["HOST", "USER", "HOSTNAME"])
        self.assertEqual(
            [line.split() for line in lines[1:]],
            [[s, getpass.getuser(), socket.gethostname()] for s in ("s1", "s2")],
        )
        obj = self.ok("get", "version")
        local = self.text(obj, "local", "code_version")
        self.assertRegex(local, r"^\S+$")
        # The test servers run from the checkout, not an installed tree: no code version.
        self.assertEqual(
            [(h["host"], h["code_version"], h["up_to_date"]) for h in self.objs(obj, "hosts")],
            [("s1", None, False), ("s2", None, False)],
        )
        rc, out, _ = self.plain("get", "version")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^local: +taskmgr \S+$")
        self.assertRegex(out, r"(?m)^s1 +none +differs$")
        self.assertIn("task update host NAME", out)

    def test_an_empty_machine_list_is_not_a_failure(self) -> None:
        """get jobs, get user and get version share one rule: exit 2 only when servers were
        asked and none answered."""
        with open(os.path.join(self.home, "machines.json"), "w") as f:
            json.dump({"servers": []}, f)
        for cmd, key in (
            (["get", "jobs"], "jobs"),
            (["get", "user"], "hosts"),
            (["get", "version"], "hosts"),
        ):
            out = self.ok(*cmd)
            self.assertEqual((self.get(out, key), self.get(out, "complete")), ([], True), cmd)

    def test_a_corrupt_round_robin_cursor_starts_over(self) -> None:
        rr = os.path.join(self.home, "rr.json")
        for junk in ("{not json", '{"cursor": "x"}', '{"cursor": 1e400}', "[1]"):
            with open(rr, "w") as f:
                _ = f.write(junk)
            out = self.ok("create", "job", self.make_pkg())
            self.assertEqual(self.get(out, "host"), "s1", junk)
        with open(rr) as f:
            self.assertEqual(json.load(f)["cursor"], 1)

    def test_options_are_not_abbreviated(self) -> None:
        """--plain and -o json are recognized before parsing, so only exact spellings."""
        pkg = self.make_pkg()
        for argv in (
            ["validate", "package", pkg, "--out", "json"],
            ["validate", "package", pkg, "--pla"],
            ["get", "jobs", "--hos", "s1"],
        ):
            rc, _, err = self.task(*argv, text=True)
            self.assertEqual(rc, 2, argv)
            self.assertIn("unrecognized arguments", err, argv)
        rc, _, err = self.task("create", "host", "x", "--ss", "u@h", text=True)
        self.assertEqual(rc, 2)
        self.assertIn("unrecognized arguments", err)

    def test_jobs_state_must_be_a_job_state(self) -> None:
        rc, _, err = self.task("get", "jobs", "--state", "bogus", text=True)
        self.assertEqual(rc, 2)
        self.assertIn("invalid choice: 'bogus'", err)
        self.assertEqual(self.get(self.ok("get", "jobs", "--state", "succeeded"), "jobs"), [])


class RemoteHelpTest(unittest.TestCase):
    """`taskmgr-remote -h`: every subcommand and every option has a help text."""

    def test_every_subcommand_and_option_has_help(self) -> None:
        def check(p: argparse.ArgumentParser, where: str) -> None:
            for a in p._actions:
                if isinstance(a, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                    for c in a._get_subactions():
                        self.assertTrue(c.help, "%s %s" % (where, c.dest))
                    subs = cast(Mapping[str, argparse.ArgumentParser], a.choices)
                    for name, sub in subs.items():
                        check(sub, "%s %s" % (where, name))
                else:
                    self.assertTrue(a.help, "%s %s" % (where, a.dest))

        check(remote.build_parser(), "taskmgr-remote")
