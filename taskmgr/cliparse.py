"""The command line of `task`: parsed arguments, the command table type, the arguments of the
job and package commands, and argparse with the help layout of `task -h` (usage line,
description, bold sections, colored usage errors).

The command line is kubectl-style, `task <verb> <resource> [args]` (`task get jobs`,
`task create host NAME`, `task get version`). The job, host and package resources also
have a plural name and either works (`task get job ID`, `task get jobs`).
"""

# Args declares the attributes argparse sets; their defaults come from the parser.
# pyright: reportUninitializedInstanceVariable=false

import argparse
import sys
import textwrap
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NamedTuple, NoReturn, cast

from . import textout
from .common import ALL_STATES, override
from .textout import BOLD, CYAN, RED, CmdResult, hint, paint


class Args(argparse.Namespace):
    """Parsed command line: the union of every command's attributes.

    argparse sets `home`, `verb`, `resource` and the attributes of the chosen command, with
    the parser's defaults; the class defaults below cover what some commands lack.
    """

    home: str | None
    verb: str
    resource: str
    output: str = "text"  # -o; the parsers default to SUPPRESS, so this is the default
    plain: bool = False  # --plain defaults to SUPPRESS (see build_parser)
    # job commands
    jobid: str  # "" for `get jobs` without an ID
    host: str | None
    submission_key: str | None
    state: str | None
    limit: int | None
    poll_interval: float
    retry_timeout: float
    timeout: float | None
    as_: str | None
    confirm: str | None
    stream: str
    follow: bool
    # package commands
    task_id: str
    path: str
    dest: str | None  # create package, build package|pyz, fetch job
    sources: list[str]
    entry: str | None
    precompile: str | None
    precompile_timeout: int | None
    gpus: str | None
    venv: str | None
    require_path: list[str] | None
    env: list[str] | None
    output_key: list[str] | None
    task_md: str | None
    with_cute_helpers: bool
    exclude: list[str] | None
    force: bool
    # host commands
    name: str  # "" for `get hosts` / `ping host` without a NAME
    ssh: str | None
    port: int | None
    identity_file: str | None
    ssh_option: list[str] | None
    enabled: bool | None
    pyz: str | None
    yes: bool
    plan: bool
    prune_old_code: bool


Setup = Callable[[argparse.ArgumentParser], str | None]


class Command(NamedTuple):
    verb: str
    resource: str  # singular name
    shown: str  # what `task -h` shows after the verb, e.g. "jobs [ID]"
    group: str  # heading in `task -h`
    help: str
    setup: Setup  # adds the command's arguments; returns its description (None: help)
    run: Callable[[Args], CmdResult]
    text: Callable[[Args, Mapping[str, object], bool], str] | None  # None: streams text itself

    @property
    def name(self) -> str:
        return "%s %s" % (self.verb, self.resource)


# Plural names, accepted like the singular ones.
PLURALS = {"job": "jobs", "host": "hosts", "package": "packages"}
# The commands of the old command line and what replaced them: a usage error names the
# new form (the old forms are not aliases).
RENAMED = {
    "submit": "`task create job PKG`",
    "jobs": "`task get jobs`",
    "status": "`task get job ID` (with --wait: `task wait job ID`)",
    "init": "`task create package TASK_ID`",
    "pack": "`task build package SRC...`",
    "host": "`task <create|get|update|delete|enable|disable|ping> host NAME`",
    "whoami": "`task get user`",
    "version": "`task get version`",
}
# Old commands whose word is still a verb: the verb without a resource names the new form.
RENAMED_VERBS = {"update": "`task update cli` (a server: `task update host NAME`)"}

HelpEntry = tuple[str, str]  # (name column, help text)
HELP_WIDTH = 88
OUTPUT_HELP = "output format: text (default) or json, the full JSON object"
LOGS_OUTPUT_HELP = "the log is always raw text; json also prints an error as a JSON object"
PLAIN_HELP = "no colors (also NO_COLOR=1)"
HOME_HELP = "local state directory (default $TASKMGR_HOME or ~/.taskmgr)"
TOP_USAGE = "task <command> <resource> [args]"
# Placement of the common options at the end of every option list and usage line.
COMMON_ORDER = {"-o": 1, "--plain": 2, "-h": 3}


def render_help(
    usage: str, description: str, sections: Sequence[tuple[str, Sequence[HelpEntry]]], footer: str
) -> str:
    """The layout of every `-h`: usage line, description, then bold sections of cyan names
    and help text in one aligned column, then a footer line."""
    width = max((len(n) for _, entries in sections for n, _ in entries), default=0) + 2
    text = usage_line(usage) + "\n"
    if description:
        text += textwrap.fill(description, HELP_WIDTH) + "\n\n"

    for title, entries in sections:
        if not entries:
            continue
        text += paint(title, BOLD) + "\n"
        for name, help_text in entries:
            body = textwrap.wrap(help_text, max(20, HELP_WIDTH - width - 2)) or [""]
            text += "  %s%s\n" % (paint(name.ljust(width), CYAN), body[0])
            text += "".join("  %s%s\n" % (" " * width, more) for more in body[1:])
        text += "\n"
    return text + footer + "\n"


def usage_line(usage: str) -> str:
    """`usage: <usage>`; long lines wrap under the first argument."""
    words = _usage_words(usage)
    n_prog = next((i for i, w in enumerate(words) if w.startswith(("<", "["))), len(words))
    indent = " " * (len(" ".join(words[:n_prog])) + 1)

    lines: list[str] = []
    cur = ""
    for w in words:
        if cur.strip() and len(cur) + 1 + len(w) > HELP_WIDTH - 7:
            lines.append(cur)
            cur = indent + w
        else:
            cur = (cur + " " + w) if cur else w
    lines.append(cur)
    return "%s %s\n" % (paint("usage:", BOLD), ("\n" + " " * 7).join(lines))


def _usage_words(usage: str) -> list[str]:
    """Split a usage string into words, keeping `[--opt VALUE]` groups together."""
    words: list[str] = []
    depth = 0
    for w in usage.split(" "):
        if depth and words:
            words[-1] += " " + w
        else:
            words.append(w)
        depth += w.count("[") - w.count("]")
    return words


def top_help(commands: Sequence[Command]) -> str:
    """`task -h`: the commands by group."""
    groups: dict[str, list[HelpEntry]] = {}
    for c in commands:
        name = c.verb + (" " + c.shown if c.shown else "")
        groups.setdefault(c.group, []).append((name, c.help))

    groups["options"] = [
        ("-o, --output text|json", OUTPUT_HELP + " (logs: the log is always raw text)"),
        ("--plain", PLAIN_HELP),
        ("--home DIR", HOME_HELP),
        ("-h, --help", "show this help"),
    ]

    return render_help(
        TOP_USAGE,
        "Run task packages on GPU servers and follow their jobs. A command is a verb and a "
        + "resource; job, host and package also have a plural name (jobs, hosts, packages).",
        list(groups.items()),
        "`task <command> -h` lists a command's resources; "
        + "`task <command> <resource> -h` shows its arguments and options.",
    )


def _opt_value(a: argparse.Action) -> str:
    """The value placeholder of an option: its metavar, choices or DEST."""
    if a.nargs == 0:
        return ""
    if isinstance(a.metavar, str):
        return a.metavar
    if a.choices is not None:
        choices = cast(Sequence[object], a.choices)
        return "|".join(str(c) for c in choices)
    return a.dest.upper()


def sentence(text: str) -> str:
    """Capitalized and ending with a period, like the description in `task -h`."""
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")


def positive_int(text: str) -> int:
    """argparse type: an integer >= 1."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer, got %r" % text)
    return value


class Parser(argparse.ArgumentParser):
    """argparse with the help layout of `task -h`, colored usage errors, and usage errors
    that name the new form of an old command."""

    def _parts(self) -> tuple[list[str], list[HelpEntry], list[HelpEntry], list[HelpEntry]]:
        """(usage words, resources, arguments, options) of this parser."""
        usage: list[tuple[str, str]] = []  # (word, primary option string or "")
        resources: list[HelpEntry] = []
        arguments: list[HelpEntry] = []
        options: list[tuple[HelpEntry, str]] = []

        for a in self._actions:
            if a.help == argparse.SUPPRESS and not a.option_strings:
                continue
            help_text = a.help if isinstance(a.help, str) and a.help != argparse.SUPPRESS else ""
            if isinstance(a, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                usage.append(("<resource>" if a.required else "[<resource>]", ""))
                for c in a._get_subactions():
                    shown = c.metavar if isinstance(c.metavar, str) else c.dest
                    resources.append((shown, c.help if isinstance(c.help, str) else ""))
            elif not a.option_strings:
                name = "<%s>" % (a.metavar if isinstance(a.metavar, str) else a.dest)
                if a.nargs == "*":
                    name += "..."
                usage.append(("[%s]" % name if a.nargs in ("?", "*") else name, ""))
                arguments.append((name, help_text))
            elif a.help != argparse.SUPPRESS:
                value = _opt_value(a)
                flag = a.option_strings[0]  # the primary name (--plan, not --dry-run)
                shown = (flag + " " + value).strip()
                if isinstance(a, argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
                    help_text = "show this help"
                else:
                    usage.append((shown if a.required else "[%s]" % shown, flag))
                names = ", ".join(sorted(a.option_strings, key=len))
                options.append((((names + " " + value).strip(), help_text), flag))

        # Like `task -h`: the command's own options, then -o, --plain, and -h last.
        options.sort(key=lambda e: COMMON_ORDER.get(e[1], 0))
        usage.sort(key=lambda w: (w[0].startswith("["), COMMON_ORDER.get(w[1], 0)))
        return [w for w, _ in usage], resources, arguments, [e for e, _ in options]

    def _usage(self) -> str:
        words, _, _, _ = self._parts()
        return " ".join([self.prog] + words)

    @override
    def format_help(self) -> str:
        words, resources, arguments, options = self._parts()
        footer = (
            "`%s <resource> -h` shows a resource's arguments and options." % self.prog
            if resources
            else "`task -h` lists all commands."
        )
        return render_help(
            " ".join([self.prog] + words),
            sentence(self.description or ""),
            [("resources", resources), ("arguments", arguments), ("options", options)],
            footer,
        )

    @override
    def format_usage(self) -> str:
        return usage_line(self._usage())

    @override
    def _check_value(self, action: argparse.Action, value: object) -> None:
        """An unknown command or resource, or a path given to -o: a message that tells what
        to type instead (argparse's own for every other choice)."""
        choices = cast(Iterable[object] | None, action.choices)
        if choices is None or value in choices:
            return
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            raise argparse.ArgumentError(None, self._unknown_word(str(value), choices))
        if "--output" in action.option_strings:
            raise argparse.ArgumentError(
                None,
                "-o/--output is the output format, text or json, not %r " % value
                + "(a path is given with --dest)",
            )
        super()._check_value(action, value)

    def _unknown_word(self, word: str, choices: Iterable[object]) -> str:
        if self.prog != "task":
            names = ", ".join(str(c) for c in choices)
            return "unknown resource %r for `%s` (choose from %s)" % (word, self.prog, names)
        if word in RENAMED:
            return "`task %s` is now %s" % (word, RENAMED[word])
        return "unknown command %r" % word

    @override
    def error(self, message: str) -> NoReturn:
        if "unrecognized arguments" in message and "--detail" in message:
            message += " (--detail is now -o json)"
        verb = self.prog.removeprefix("task ")
        if "required: <resource>" in message and verb in RENAMED_VERBS:
            message = "`task %s` alone is now %s" % (verb, RENAMED_VERBS[verb])
        _ = sys.stderr.write(self.format_usage())
        _ = sys.stderr.write(textout.message("error", RED, message))

        if self.prog == "task":
            what = "lists all commands"
        elif self._parts()[1]:
            what = "lists its resources"
        else:
            what = "shows its arguments and options"
        _ = sys.stderr.write(hint("`%s -h` %s" % (self.prog, what)))
        sys.exit(2)


class TopParser(Parser):
    commands: Sequence[Command] = ()

    @override
    def format_help(self) -> str:
        return top_help(self.commands)

    @override
    def format_usage(self) -> str:
        return usage_line(TOP_USAGE)


def _no_abbrev(p: argparse.ArgumentParser) -> None:
    """Turn off option abbreviations in p and its subparsers: main() looks for --plain and
    -o json in argv before parsing, which only works for the exact spellings."""
    p.allow_abbrev = False
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            for sub in cast(Mapping[str, argparse.ArgumentParser], a.choices).values():
                _no_abbrev(sub)


def _add_plain(p: argparse.ArgumentParser) -> None:
    """--plain anywhere on the line; SUPPRESS keeps a later parser from resetting it."""
    _ = p.add_argument("--plain", action="store_true", default=argparse.SUPPRESS, help=PLAIN_HELP)


def _add_output(p: argparse.ArgumentParser, streams: bool) -> None:
    """-o; SUPPRESS keeps a later parser from resetting it (Args.output is the default)."""
    _ = p.add_argument(
        "-o",
        "--output",
        choices=("text", "json"),
        default=argparse.SUPPRESS,
        help=LOGS_OUTPUT_HELP if streams else OUTPUT_HELP,
    )


def build_parser(commands: Sequence[Command], verbs: Mapping[str, str]) -> argparse.ArgumentParser:
    """The parser of every command; `verbs` maps each verb to its one-line help."""
    p = TopParser(prog="task")
    p.commands = commands
    _ = p.add_argument("--home", help=HOME_HELP)
    _add_plain(p)
    sub = p.add_subparsers(dest="verb", required=True, metavar="<command>", parser_class=Parser)

    for verb, verb_help in verbs.items():
        vp = sub.add_parser(verb, help=verb_help, description=verb_help)
        _add_plain(vp)

        rs = vp.add_subparsers(
            dest="resource", required=True, metavar="<resource>", parser_class=Parser
        )
        for c in (c for c in commands if c.verb == verb):
            plural = PLURALS.get(c.resource)
            rp = rs.add_parser(c.resource, aliases=[plural] if plural else [], help=c.help)
            rp.description = c.setup(rp) or c.help
            _add_output(rp, c.text is None)
            _add_plain(rp)
            rp.set_defaults(resource=c.resource)  # the singular name, also when typed plural

    _no_abbrev(p)
    return p


# ---------------------------------------------------------------------------
# Arguments of the job and package commands (the host commands: hostcmds)

JOBID_HELP = "the job's id (16 hex digits)"
HOST_HELP = "server name (default: look the jobid up on every server)"
ALL_HOSTS_HELP = "only this server (default: every server)"
DEFAULT_JOBS_LIMIT = 10


def _job_arguments(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("jobid", metavar="ID", help=JOBID_HELP)
    _ = p.add_argument("--host", help=HOST_HELP)


def args_create_job(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("path", metavar="PKG", help="package directory, .tar.gz or .tgz")
    _ = p.add_argument("--host", help="server name (default: round-robin over enabled servers)")
    _ = p.add_argument(
        "--submission-key",
        metavar="KEY",
        help="idempotency key: the same key on the same server returns the same job "
        + "(needs --host)",
    )
    return (
        "Submit a task package to a server as a new job. The package is validated first; if "
        + "it uses a feature the server's code lacks, it is refused before the upload with "
        + "unsupported_feature: run `task update host HOST` to update the server."
    )


def args_get_job(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument(
        "jobid", metavar="ID", nargs="?", default="", help=JOBID_HELP + " (default: list jobs)"
    )
    _ = p.add_argument("--host", help="only this server; with ID: the server of the job")
    _ = p.add_argument(
        "--state",
        choices=ALL_STATES,
        metavar="STATE",
        help="list only jobs in this state: %s" % ", ".join(ALL_STATES),
    )
    _ = p.add_argument(
        "-n",
        dest="limit",
        type=positive_int,
        metavar="N",
        help="list the N most recently submitted jobs, over all servers (default %d)"
        % DEFAULT_JOBS_LIMIT,
    )

    return (
        "Without ID: list the jobs on every enabled server, oldest first. With ID: show that "
        + "job. A job waiting for GPUs lists the processes that hold them on the server "
        + "(gpu_holders: pid, user, command, GPU memory); a cancelled job shows who asked and "
        + "when (cancel_request). --state and -n only apply to the list."
    )


def args_wait_job(p: argparse.ArgumentParser) -> str:
    _job_arguments(p)
    _ = p.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="seconds between polls (default 5)",
    )
    _ = p.add_argument(
        "--retry-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="give up after this long without a connection (default 300)",
    )
    _ = p.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="give up after this long (default: no limit)",
    )

    return (
        "Wait until a job ends, then show it like `task get job ID`; an interrupted job is "
        + "followed to its automatic re-run. Exit 0: succeeded, 1: ended otherwise, "
        + "2: --timeout or no connection, 3: the job is unknown (see `task resolve job`)."
    )


def args_logs_job(p: argparse.ArgumentParser) -> str:
    _job_arguments(p)
    _ = p.add_argument(
        "--stream",
        choices=("stdout", "stderr", "shim"),
        default="stdout",
        help="which log (default stdout)",
    )
    _ = p.add_argument("--follow", action="store_true", help="keep printing until the job ends")
    return "Print a job's stdout, stderr or shim log as it is (raw text, also with -o json)."


def args_fetch_job(p: argparse.ArgumentParser) -> None:
    _job_arguments(p)
    _ = p.add_argument(
        "--dest",
        required=True,
        metavar="DIR",
        help="download into DIR/<host>/<jobid>/ (must not exist yet)",
    )


def args_cancel_job(p: argparse.ArgumentParser) -> str:
    _job_arguments(p)
    return (
        "Cancel a job. The server records who asked (this CLI's user@hostname) and when; "
        + "`task get job ID` shows it as cancel_request."
    )


def args_retry_job(p: argparse.ArgumentParser) -> None:
    _job_arguments(p)


def args_resolve_job(p: argparse.ArgumentParser) -> None:
    _job_arguments(p)
    _ = p.add_argument(
        "--as",
        dest="as_",
        choices=("stopped",),
        help="declare that the job's processes are gone (needs --confirm)",
    )
    _ = p.add_argument("--confirm", metavar="JOBID", help="repeat the jobid to confirm --as")


def args_create_package(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("task_id", help="task id, e.g. AA-01 (two capitals, dash, 2-4 digits)")
    _ = p.add_argument("--dest", metavar="DIR", help="target directory (default ./<task_id>)")
    return "Create a task package from the template, to fill in by hand."


def args_build_package(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument(
        "sources", nargs="*", metavar="SRC", help="file or directory to copy; SRC=DEST to place it"
    )

    _ = p.add_argument("--task-id", help="task id, e.g. AA-01 (required without a task.json)")
    _ = p.add_argument(
        "--entry",
        metavar="CMD",
        help="entry command, split like a shell would (no shell runs it), "
        + "e.g. 'python3 run.py --cfg a.yaml'",
    )
    _ = p.add_argument("--precompile", metavar="CMD", help="precompile command, split like --entry")
    _ = p.add_argument(
        "--precompile-timeout", type=positive_int, metavar="SECONDS", help="precompile time limit"
    )
    _ = p.add_argument(
        "--gpus",
        metavar="SPEC",
        help="none, a list such as 0,1 or GPU-..., or JSON such as "
        + '\'{"count": 1, "min_free_gb": 40}\' (default none)',
    )
    _ = p.add_argument("--venv", metavar="PATH", help="absolute path of the venv on the server")
    _ = p.add_argument(
        "--require-path",
        action="append",
        metavar="PATH",
        help="absolute path that must exist on the server (repeatable)",
    )
    _ = p.add_argument(
        "--env", action="append", metavar="NAME=VALUE", help="environment variable (repeatable)"
    )
    _ = p.add_argument("--timeout", type=positive_int, metavar="SECONDS", help="entry time limit")
    _ = p.add_argument(
        "--output-key",
        action="append",
        metavar="KEY",
        help="key result.json outputs must have (repeatable)",
    )

    _ = p.add_argument("--task-md", metavar="FILE", help="use FILE as TASK.md")
    _ = p.add_argument(
        "--with-cute-helpers",
        action="store_true",
        help="also copy precompile.py and cutecache.py from the template",
    )
    _ = p.add_argument(
        "--exclude",
        action="append",
        metavar="GLOB",
        help="leave out names matching GLOB when copying a directory (repeatable)",
    )

    _ = p.add_argument(
        "--dest",
        metavar="PATH",
        help="where to write the package: a directory, or a file ending in .tar.gz or .tgz "
        + "(default ./<task_id>)",
    )
    _ = p.add_argument(
        "--force",
        action="store_true",
        help="replace an existing --dest that holds an earlier package (a directory with a "
        + "task.json, or a .tar.gz / .tgz file); refused for the current directory, a source "
        + "or a directory that holds one",
    )

    return (
        "Build a task package from project files and options, validate it, and print its "
        + "path on stdout (a directory, or an archive when --dest ends in .tar.gz or .tgz), "
        + 'so that `task create job "$(task build package ...)"` works. A directory SRC is '
        + "copied into the package root, a file SRC to the root under its name, SRC=DEST to "
        + "DEST in the package; symlinks are followed. A task.json among the sources is the "
        + "base the options change; TASK.md and progress.json are written when missing; "
        + "taskmgr_task.py comes from the installed template. Exit 0 valid, 1 invalid "
        + "(errors listed), 2 bad arguments."
    )


def args_validate_package(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("path", metavar="PATH", help="package directory, .tar.gz or .tgz")
    return (
        "Check a task package directory or archive. Keys task.json does not define are errors "
        + "(keys starting with x- are annotations); -o json lists the server features the "
        + "package needs (`features`)."
    )


def args_build_pyz(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument(
        "--dest",
        metavar="DIR",
        help="output directory (default: dist/ in the checkout, where `task create host` "
        + "looks for taskmgr-remote.pyz; required when task runs from a .pyz)",
    )
    return (
        "Build task.pyz (this CLI) and taskmgr-remote.pyz (the server side) from this code: "
        + "single-file zipapps that need only python3 >= 3.10. Existing files are replaced."
    )


def args_all_hosts(p: argparse.ArgumentParser) -> None:
    _ = p.add_argument("--host", help=ALL_HOSTS_HELP)


def args_none(_p: argparse.ArgumentParser) -> None:
    return None
