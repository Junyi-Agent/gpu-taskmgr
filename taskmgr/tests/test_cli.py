"""Text output (the default), --detail, the grouped help, jobid lookup without --host,
whoami and version."""

import getpass
import json
import os
import re
import socket

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

    def test_help_lists_commands_by_group(self) -> None:
        rc, out, _ = self.plain("-h")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("usage: task <command> [args]\n"), out)
        heads = [line for line in out.splitlines() if re.fullmatch("[a-z]+", line)]
        self.assertEqual(heads, ["tasks", "hosts", "others", "options"])
        names = re.findall(r"^  ([a-z]+(?: [a-z]+)?)  ", out, re.M)
        for name in ("init", "validate", "submit", "jobs", "status", "logs", "fetch"):
            self.assertIn(name, names)
        for name in ("cancel", "retry", "resolve", "whoami", "update", "version"):
            self.assertIn(name, names)
        for name in ("host add", "host list", "host remove", "host ping"):
            self.assertIn(name, names)
        # The old commands are gone.
        self.assertEqual(self.plain("hosts")[0], 2)
        self.assertEqual(self.plain("add", "host", "x")[0], 2)

    def test_every_help_has_the_same_layout(self) -> None:
        """`task -h` and every `task <command> -h`: usage line, description sentence, bold
        sections of (cyan name, help) entries, -h last, a footer naming the next help."""
        _, top, _ = self.plain("-h")
        names: list[str] = re.findall(r"(?m)^  ([a-z]+(?: [a-z]+)?)  ", top.split("\noptions\n")[0])
        self.assertIn("host add", names)
        cmds: list[list[str]] = [[], ["host"]] + [n.split() for n in names]
        for cmd in cmds:
            rc, out, err = self.plain(*cmd, "-h")
            self.assertEqual((rc, err), (0, ""), cmd)
            blocks = out.rstrip("\n").split("\n\n")
            self.assertTrue(blocks[0].startswith(" ".join(["usage: task", *cmd])), cmd)
            self.assertRegex(blocks[1], r"^[A-Z][\s\S]*\.$", cmd)  # description sentence
            self.assertRegex(blocks[-1], r"^`task [^`]*-h` [^\n]*\.$", cmd)  # footer
            titles = [sec.split("\n")[0] for sec in blocks[2:-1]]
            if cmd:
                order = ["commands", "arguments", "options"]
                self.assertEqual(titles, [t for t in order if t in titles], cmd)
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

    def test_usage_errors_point_to_the_help(self) -> None:
        cases: list[tuple[list[str], str, str]] = [
            ([], "the following arguments are required: <command>", "`task -h` lists all commands"),
            (["bogus"], "invalid choice: 'bogus'", "`task -h` lists all commands"),
            (["host"], "required: <subcommand>", "`task host -h` lists its subcommands"),
            (["retry"], "required: jobid", "`task retry -h` shows its arguments and options"),
            (
                ["host", "remove"],
                "required: name",
                "`task host remove -h` shows its arguments and options",
            ),
        ]
        for argv, error, hint in cases:
            rc, out, err = self.plain(*argv)
            self.assertEqual((rc, out), (2, ""), argv)
            usage, err_line, hint_line = err.splitlines()[-3:]
            self.assertTrue(usage.startswith("usage: task"), argv)
            self.assertIn(error, err_line, argv)
            self.assertTrue(err_line.startswith("error: "), argv)
            self.assertEqual(hint_line, "hint: " + hint, argv)

    def test_removed_attempt_option_is_a_usage_error(self) -> None:
        rc, out, err = self.plain("logs", "0123456789abcdef", "--attempt", "1")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("--attempt", err)

    def test_validate_and_init_text(self) -> None:
        target = os.path.join(self.tmp, "AA-01")
        rc, out, _ = self.plain("init", "AA-01", "--output", target)
        self.assertEqual(rc, 0)
        self.assertIn("created: AA-01 in %s\nhint: fill every" % target, out)
        rc, out, err = self.plain("validate", target)
        self.assertEqual((rc, err), (1, ""))
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^invalid: \d+ errors$")
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertIn("placeholder", out)
        rc, out, _ = self.plain("validate", self.make_pkg(task_id="BB-02"))
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^valid: BB-02, content_hash \S+\n$")
        rc, colored, _ = self.colored("validate", target)
        self.assertIn("\x1b[31minvalid", colored)
        # A missing path: one error line on stderr, nothing on stdout.
        rc, out, err = self.plain("validate", os.path.join(self.tmp, "nope"))
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(
            err, "error: path does not exist: %s (not_found)\n" % os.path.join(self.tmp, "nope")
        )
        # --detail: the JSON object (on errors too).
        rc, obj, _ = self.task("validate", "--detail", os.path.join(self.tmp, "nope"), text=True)
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "not_found"))

    def test_job_commands_find_the_host_by_jobid(self) -> None:
        rc, out, _ = self.plain(
            "submit", "--host", "s2", self.make_pkg(script="echo hi; " + OK_RESULT)
        )
        self.assertEqual(rc, 0)
        header, row = out.splitlines()
        self.assertEqual(header.split(), ["JOBID", "TASK_ID", "HOST", "STATE"])
        j = row.split()[0]
        _ = self.start_daemon("s2")
        _ = self.wait_state(j, "succeeded", name="s2")
        rc, out, _ = self.plain("status", j)
        self.assertEqual(rc, 0)
        fields = dict(
            (k.strip(), v.strip()) for k, v in (line.split(":", 1) for line in out.splitlines())
        )
        self.assertEqual(
            (fields["jobid"], fields["host"], fields["state"], fields["return_code"]),
            (j, "s2", "succeeded", "0"),
        )
        self.assertEqual(self.plain("logs", j), (0, "hi\n", ""))
        rc, out, _ = self.plain("fetch", j, "--output", os.path.join(self.tmp, "r"))
        self.assertEqual(rc, 0)
        self.assertIn("fetched: job %s from s2 into " % j, out)
        rc, out, _ = self.plain("retry", j)
        self.assertEqual(rc, 0)
        new = out.splitlines()[1].split()[0]
        rc, out, _ = self.plain("cancel", new)
        self.assertEqual(rc, 0)
        self.assertIn("cancel requested: job %s on s2" % new, out)
        rc, out, _ = self.plain("resolve", new)  # not unknown: nothing to do
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^host: +s2$")
        self.assertRegex(out, r"(?m)^changed: +no$")
        # The JSON objects still carry the host.
        self.assertEqual(self.get(self.ok("status", j), "host"), "s2")
        # A jobid nobody has: an error naming it.
        rc, out, err = self.plain("cancel", "0123456789abcdef")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("job 0123456789abcdef not found on any server", err)

    def test_status_text_of_a_queued_job(self) -> None:
        j = self.submit(self.make_pkg(task_id="CC-03"), host="s1")  # no daemon: stays queued
        rc, out, _ = self.colored("status", j)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[33mqueued\x1b[0m", out)
        plain = ANSI.sub("", out)
        self.assertRegex(plain, r"(?m)^task_id: +CC-03$")
        self.assertRegex(plain, r"(?m)^host: +s1$")

    def test_colors_everywhere_unless_plain(self) -> None:
        pkg = self.make_pkg(task_id="DD-04")
        # Colored on a pipe: text, JSON, errors on stderr, help, usage errors.
        rc, out, _ = self.colored("validate", pkg)
        self.assertEqual(rc, 0)
        self.assertIn("\x1b[32mvalid:\x1b[0m", out)
        rc, out, _ = self.colored("validate", pkg, "--detail")
        self.assertIn('\x1b[34m"valid"\x1b[0m: \x1b[35mtrue\x1b[0m', out)
        self.assertEqual(json.loads(ANSI.sub("", out)), self.ok("validate", pkg))
        rc, out, err = self.colored("validate", os.path.join(self.tmp, "nope"))
        self.assertEqual((rc, out), (2, ""))
        self.assertTrue(err.startswith("\x1b[31merror:\x1b[0m "), err)
        rc, out, _ = self.colored("-h")
        self.assertIn("\x1b[1mtasks\x1b[0m", out)
        rc, out, err = self.colored("retry")
        self.assertEqual(rc, 2)
        self.assertIn("\x1b[31merror:\x1b[0m the following arguments are required", err)
        # --plain anywhere on the line, or NO_COLOR, turns every color off.
        for argv in (
            ["--plain", "validate", pkg, "--detail"],
            ["validate", "--plain", pkg, "--detail"],
            ["validate", pkg, "--detail", "--plain"],
        ):
            rc, obj, err = self.task(*argv, text=True, color=True)  # stdout parsed as JSON
            self.assertEqual((rc, err), (0, ""), argv)
            self.assertEqual(obj, self.ok("validate", pkg), argv)
        for argv in (["-h", "--plain"], ["retry", "--plain"], ["validate", "--plain", "nope"]):
            _, out, err = self.colored(*argv)
            self.assertNotIn("\x1b[", out + err, argv)
        rc, out, err = self.task("validate", pkg, text=True, color=True, env={"NO_COLOR": "1"})
        self.assertEqual((rc, out), (0, ANSI.sub("", self.colored("validate", pkg)[1])))

    def test_whoami_and_version(self) -> None:
        rc, out, _ = self.plain("whoami")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["HOST", "USER", "HOSTNAME"])
        self.assertEqual(
            [line.split() for line in lines[1:]],
            [[s, getpass.getuser(), socket.gethostname()] for s in ("s1", "s2")],
        )
        obj = self.ok("version")
        local = self.text(obj, "local", "code_version")
        self.assertRegex(local, r"^\S+$")
        # The test servers run from the checkout, not an installed tree: no code version.
        self.assertEqual(
            [(h["host"], h["code_version"], h["up_to_date"]) for h in self.objs(obj, "hosts")],
            [("s1", None, False), ("s2", None, False)],
        )
        rc, out, _ = self.plain("version")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^local: +taskmgr \S+$")
        self.assertRegex(out, r"(?m)^s1 +none +differs$")
        self.assertIn("task host add NAME", out)

    def test_an_empty_machine_list_is_not_a_failure(self) -> None:
        """jobs, whoami and version share one rule: exit 2 only when servers were asked and
        none answered."""
        with open(os.path.join(self.home, "machines.json"), "w") as f:
            json.dump({"servers": []}, f)
        for cmd, key in (("jobs", "jobs"), ("whoami", "hosts"), ("version", "hosts")):
            out = self.ok(cmd)
            self.assertEqual((self.get(out, key), self.get(out, "complete")), ([], True), cmd)

    def test_a_corrupt_round_robin_cursor_starts_over(self) -> None:
        rr = os.path.join(self.home, "rr.json")
        for junk in ("{not json", '{"cursor": "x"}', '{"cursor": 1e400}', "[1]"):
            with open(rr, "w") as f:
                _ = f.write(junk)
            out = self.ok("submit", self.make_pkg())
            self.assertEqual(self.get(out, "host"), "s1", junk)
        with open(rr) as f:
            self.assertEqual(json.load(f)["cursor"], 1)

    def test_options_are_not_abbreviated(self) -> None:
        """--plain and --detail are recognized before parsing, so only exact spellings."""
        pkg = self.make_pkg()
        for argv in (
            ["validate", pkg, "--det"],
            ["validate", pkg, "--pla"],
            ["jobs", "--hos", "s1"],
        ):
            rc, _, err = self.task(*argv, text=True)
            self.assertEqual(rc, 2, argv)
            self.assertIn("unrecognized arguments", err, argv)
        rc, _, err = self.task("host", "add", "x", "--ss", "u@h", text=True)
        self.assertEqual(rc, 2)
        self.assertIn("unrecognized arguments", err)

    def test_jobs_state_must_be_a_job_state(self) -> None:
        rc, _, err = self.task("jobs", "--state", "bogus", text=True)
        self.assertEqual(rc, 2)
        self.assertIn("invalid choice: 'bogus'", err)
        self.assertEqual(self.get(self.ok("jobs", "--state", "succeeded"), "jobs"), [])
