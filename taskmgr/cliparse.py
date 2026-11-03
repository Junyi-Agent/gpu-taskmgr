"""The command line of `task`: parsed arguments, the command table, and argparse with the
help layout of `task -h` (usage line, description, bold sections, colored usage errors)."""

# Args declares the attributes argparse sets; their defaults come from the parser.
# pyright: reportUninitializedInstanceVariable=false

import argparse
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple, NoReturn, cast

from . import hostcmds, textout
from .common import ALL_STATES, override
from .textout import BOLD, CYAN, RED, CmdResult, hint, paint


class Args(argparse.Namespace):
    """Parsed command line: the union of every subcommand's attributes.

    argparse sets `home`, `cmd` and the attributes of the chosen subcommand, with the parser's
    defaults; the class defaults below only cover options that some subcommands lack.
    """

    home: str | None
    cmd: str
    task_id: str
    output: str | None
    path: str
    host: str | None
    submission_key: str | None
    state: str | None
    limit: int
    jobid: str
    wait: bool
    poll_interval: float
    retry_timeout: float
    timeout: float | None
    as_: str | None
    confirm: str | None
    stream: str
    follow: bool
    detail: bool = False  # logs has no --detail
    plain: bool = False  # --plain defaults to SUPPRESS (see build_parser)
    # host add
    host_cmd: str
    name: str
    ssh: str | None
    port: int | None
    identity_file: str | None
    ssh_option: list[str] | None
    enabled: bool | None
    pyz: str | None
    yes: bool


class Command(NamedTuple):
    name: str
    group: str  # heading in `task -h`
    help: str
    run: Callable[[Args], CmdResult]
    text: Callable[[Args, Mapping[str, object], bool], str] | None  # None: streams text itself


HelpEntry = tuple[str, str]  # (name column, help text)
HELP_WIDTH = 88
DETAIL_HELP = "print the full JSON object instead of text"
PLAIN_HELP = "no colors (also NO_COLOR=1)"


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
        entries = groups.setdefault(c.group, [])
        if c.name == "host":
            entries.extend(("host " + n, h) for n, h in hostcmds.HELP)
        else:
            entries.append((c.name, c.help))
    groups["options"] = [
        ("--detail", DETAIL_HELP + " (all but logs)"),
        ("--plain", PLAIN_HELP),
        ("--home DIR", "local state directory (default $TASKMGR_HOME or ~/.taskmgr)"),
        ("-h, --help", "show this help"),
    ]
    return render_help(
        "task <command> [args]",
        "Run task packages on GPU servers and follow their jobs.",
        list(groups.items()),
        "`task <command> -h` shows a command's arguments and options.",
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
    """argparse with the help layout of `task -h` and colored usage errors."""

    def _parts(self) -> tuple[list[str], list[HelpEntry], list[HelpEntry], list[HelpEntry]]:
        """(usage words, commands, arguments, options) of this parser."""
        usage: list[str] = []
        commands: list[HelpEntry] = []
        arguments: list[HelpEntry] = []
        options: list[HelpEntry] = []
        for a in self._actions:
            if a.help == argparse.SUPPRESS and not a.option_strings:
                continue
            help_text = a.help if isinstance(a.help, str) and a.help != argparse.SUPPRESS else ""
            if isinstance(a, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                usage.append("<subcommand>")
                for c in a._get_subactions():
                    commands.append((c.dest, c.help if isinstance(c.help, str) else ""))
            elif not a.option_strings:
                name = "<%s>" % (a.metavar if isinstance(a.metavar, str) else a.dest)
                usage.append("[%s]" % name if a.nargs == "?" else name)
                arguments.append((name, help_text))
            elif a.help != argparse.SUPPRESS:
                value = _opt_value(a)
                flag = max(a.option_strings, key=len)
                shown = (flag + " " + value).strip()
                if not isinstance(a, argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
                    usage.append(shown if a.required else "[%s]" % shown)
                names = ", ".join(sorted(a.option_strings, key=len))
                if isinstance(a, argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
                    help_text = "show this help"
                options.append(((names + " " + value).strip(), help_text))
        # Like `task -h`: the command's own options, then --detail, --plain, and -h last.
        order = {"--detail": 1, "--plain": 2, "-h, --help": 3}
        options.sort(key=lambda e: order.get(e[0], 0))
        usage.sort(key=lambda w: (w.startswith("["), order.get(w.strip("[]"), 0)))
        return usage, commands, arguments, options

    def _usage(self) -> str:
        words, _, _, _ = self._parts()
        return " ".join([self.prog] + words)

    @override
    def format_help(self) -> str:
        words, commands, arguments, options = self._parts()
        footer = (
            "`%s <subcommand> -h` shows a subcommand's arguments and options." % self.prog
            if commands
            else "`task -h` lists all commands."
        )
        return render_help(
            " ".join([self.prog] + words),
            sentence(self.description or ""),
            [("commands", commands), ("arguments", arguments), ("options", options)],
            footer,
        )

    @override
    def format_usage(self) -> str:
        return usage_line(self._usage())

    @override
    def error(self, message: str) -> NoReturn:
        _ = sys.stderr.write(self.format_usage())
        _ = sys.stderr.write(textout.message("error", RED, message))
        if self.prog == "task":
            what = "lists all commands"
        elif self._parts()[1]:
            what = "lists its subcommands"
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
        return usage_line("task <command> [args]")


def _no_abbrev(p: argparse.ArgumentParser) -> None:
    """Turn off option abbreviations in p and its subparsers: main() looks for --plain and
    --detail in argv before parsing, which only works for the exact spellings."""
    p.allow_abbrev = False
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            for sub in cast(Mapping[str, argparse.ArgumentParser], a.choices).values():
                _no_abbrev(sub)


def build_parser(commands: Sequence[Command]) -> argparse.ArgumentParser:
    by_name = {c.name: c for c in commands}
    p = TopParser(prog="task")
    p.commands = commands
    _ = p.add_argument("--home", help="local state directory (default $TASKMGR_HOME or ~/.taskmgr)")
    # --plain anywhere on the line; SUPPRESS keeps a subcommand from resetting it.
    plain = Parser(add_help=False)
    _ = plain.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help=PLAIN_HELP,
    )
    _ = p.add_argument("--plain", action="store_true", default=argparse.SUPPRESS, help=PLAIN_HELP)
    # --detail / --plain on every subcommand that prints a result.
    common = Parser(add_help=False, parents=[plain])
    _ = common.add_argument("--detail", action="store_true", help=DETAIL_HELP)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>", parser_class=Parser)

    def add(name: str, description: str | None = None) -> argparse.ArgumentParser:
        c = by_name[name]
        # `host` puts the options on its subcommands; logs prints the log itself.
        parents = [plain] if name == "host" or c.text is None else [common]
        return sub.add_parser(name, help=c.help, description=description or c.help, parents=parents)

    jobid_help = "the job's id (16 hex digits)"
    host_help = "server name (default: look the jobid up on every server)"
    s = add("init")
    _ = s.add_argument("task_id", help="task id, e.g. AA-01 (two capitals, dash, 2-4 digits)")
    _ = s.add_argument("--output", metavar="DIR", help="target directory (default ./<task_id>)")
    s = add(
        "validate",
        "Check a task package directory or tar.gz. Keys task.json does not define are errors "
        + "(keys starting with x- are annotations); --detail lists the server features the "
        + "package needs (`features`).",
    )
    _ = s.add_argument("path", help="package directory or .tar.gz")
    s = add(
        "submit",
        "Submit a task package to a server. The package is validated first; if it uses a "
        + "feature the server's code lacks (e.g. precompile on an older server), it is refused "
        + "before the upload with unsupported_feature: run `task host add HOST` to update "
        + "the server.",
    )
    _ = s.add_argument("path", help="package directory or .tar.gz")
    _ = s.add_argument("--host", help="server name (default: round-robin over enabled servers)")
    _ = s.add_argument(
        "--submission-key",
        metavar="KEY",
        help="idempotency key: the same key on the same server returns the same job "
        + "(needs --host)",
    )
    s = add("jobs")
    _ = s.add_argument("--host", help="only this server (default: every server)")
    _ = s.add_argument(
        "--state",
        choices=ALL_STATES,
        metavar="STATE",
        help="only jobs in this state: %s" % ", ".join(ALL_STATES),
    )
    _ = s.add_argument(
        "-n",
        dest="limit",
        type=positive_int,
        default=10,
        metavar="N",
        help="show the N most recently submitted jobs, over all servers (default 10)",
    )
    s = add("status")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--wait", action="store_true", help="wait until the job ends; the exit code tells how"
    )
    _ = s.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="--wait: seconds between polls (default 5)",
    )
    _ = s.add_argument(
        "--retry-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="--wait: give up after this long without a connection (default 300)",
    )
    _ = s.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="--wait: give up after this long (default: no limit)",
    )
    s = add("logs")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--stream",
        choices=("stdout", "stderr", "shim"),
        default="stdout",
        help="which log (default stdout)",
    )
    _ = s.add_argument("--follow", action="store_true", help="keep printing until the job ends")
    s = add("fetch")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--output",
        required=True,
        metavar="DIR",
        help="download into DIR/<host>/<jobid>/ (must not exist yet)",
    )
    for name in ("cancel", "retry"):
        s = add(name)
        _ = s.add_argument("jobid", help=jobid_help)
        _ = s.add_argument("--host", help=host_help)
    s = add("resolve")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--as",
        dest="as_",
        choices=("stopped",),
        help="declare that the job's processes are gone (needs --confirm)",
    )
    _ = s.add_argument("--confirm", metavar="JOBID", help="repeat the jobid to confirm --as")
    hostcmds.add_parsers(add("host"), common)
    s = add("whoami")
    _ = s.add_argument("--host", help="only this server (default: every server)")
    _ = add("update")
    s = add("version")
    _ = s.add_argument("--host", help="only this server (default: every server)")
    _no_abbrev(p)
    return p
