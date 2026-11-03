"""`task host add|list|remove|ping`: manage the machine list ($TASKMGR_HOME/machines.json).

Interface used by taskmgr.py:
    HELP            [(subcommand, one-line help)], also shown in `task -h`
    add_parsers()   adds the subcommands (dest "host_cmd") under the `host` parser
    run()           runs one subcommand: (JSON object, exit code)
    render()        the colored text printed instead of the JSON (without --detail)
"""

import argparse
import concurrent.futures
import os
import time
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from .common import TaskmgrError
from .hostsetup import STEP_COLORS, AddHostArgs, add_host, destination
from .prompt import Declined, NonInteractive, Ui
from .records import JsonValue, Server
from .textout import BOLD, GREEN, RED, YELLOW, CmdResult, cell, hint, message, render_table
from .textout import rows as _rows
from .transport import Remote, edit_machines, load_machines

HELP = [
    ("add", "set up or update a server and add it to the machine list"),
    ("list", "list the configured servers"),
    ("remove", "remove a server from the machine list"),
    ("enable", "use a server again for submits and queries"),
    ("disable", "leave a server out of submits and queries for now"),
    ("ping", "check that servers answer"),
]
PING_TIMEOUT = 60.0


class HostArgs(AddHostArgs, Protocol):
    host_cmd: str


def add_parsers(host: argparse.ArgumentParser, common: argparse.ArgumentParser) -> None:
    sub = host.add_subparsers(dest="host_cmd", required=True, metavar="<subcommand>")
    helps = dict(HELP)
    h = sub.add_parser(
        "add",
        parents=[common],
        help=helps["add"],
        description="Check the server step by step and change only what differs from the "
        + "wanted state (code, remote_root, systemd user service, linger, machine list). "
        + "Every change is confirmed with [y/N]; a second run changes nothing. --yes answers "
        + "yes to every change except deleting code of older versions outside remote_root "
        + "(step old_code): that needs --prune-old-code or a y typed in a terminal, otherwise "
        + "it is kept. --plan runs every check and prints what would change, changing nothing "
        + "(exit 0: nothing to change, 1: changes planned, 2: a check failed). "
        + "The server's storage root is $TASKMGR_REMOTE_ROOT (default: the existing entry's, "
        + "else ~/.taskmgr-remote on the server); it holds config.json, the job data and "
        + "the code.",
    )
    _ = h.add_argument("name", help="name in the machine list")
    _ = h.add_argument("--ssh", metavar="USER@HOST", help="ssh destination (required when new)")
    _ = h.add_argument("--port", type=int, help="ssh port (default 22)")
    _ = h.add_argument("--identity-file", metavar="FILE", help="ssh private key file")
    _ = h.add_argument(
        "--ssh-option",
        action="append",
        metavar="KEY=VALUE",
        help="extra `ssh -o` option (repeatable); replaces an option with the same key",
    )
    g = h.add_mutually_exclusive_group()
    _ = g.add_argument(
        "--disabled",
        dest="enabled",
        action="store_false",
        default=None,
        help="keep the server out of round-robin submits",
    )
    _ = g.add_argument(
        "--enabled",
        dest="enabled",
        action="store_true",
        default=None,
        help="put the server back into round-robin submits",
    )
    _ = h.add_argument(
        "--pyz",
        metavar="FILE",
        help="taskmgr-remote.pyz to upload, built by `python3 -m taskmgr.pyz` (default: "
        + "dist/taskmgr-remote.pyz in the checkout, or next to task.pyz); its code version "
        + "must equal this task's",
    )
    _ = h.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="answer yes to every prompt, except deletions (see --prune-old-code)",
    )
    _ = h.add_argument(
        "--plan",
        "--dry-run",
        action="store_true",
        help="check everything and print every change it would make; change nothing",
    )
    _ = h.add_argument(
        "--prune-old-code",
        action="store_true",
        help="delete the code older versions installed outside remote_root (~/taskmgr, "
        + "~/taskmgr.old-*, ~/taskmgr-remote.pyz); never done by --yes alone",
    )
    _ = sub.add_parser("list", parents=[common], help=helps["list"], description=helps["list"])
    h = sub.add_parser(
        "remove",
        parents=[common],
        help=helps["remove"],
        description="Remove a server from the local machine list. The server itself (code, "
        + "service, remote_root and its jobs) is not touched; `task host add` adds it again.",
    )
    _ = h.add_argument("name", help="name in the machine list")
    _ = h.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    h = sub.add_parser(
        "enable",
        parents=[common],
        help=helps["enable"],
        description="Mark a server enabled in the machine list: round-robin submits and the "
        + "queries over every server (jobs, the jobid lookup, whoami, version) use it again.",
    )
    _ = h.add_argument("name", help="name in the machine list")
    h = sub.add_parser(
        "disable",
        parents=[common],
        help=helps["disable"],
        description="Mark a server disabled in the machine list, e.g. while it is down: "
        + "round-robin submits skip it and the queries over every server (jobs, the jobid "
        + "lookup, whoami, version, host ping) do not wait for it. `--host NAME` and "
        + "`task host ping NAME` still reach it. Nothing changes on the server; its jobs keep "
        + "running.",
    )
    _ = h.add_argument("name", help="name in the machine list")
    h = sub.add_parser(
        "ping",
        parents=[common],
        help=helps["ping"],
        description="Run `taskmgr-remote version` on each enabled server (in parallel), or on "
        + "the named one even if it is disabled, and report "
        + "whether it answers, the round-trip time and the installed code version. "
        + "Exit 0 when every pinged server answered, 2 otherwise.",
    )
    _ = h.add_argument("name", nargs="?", default="", help="one server (default: all)")


def run(args: HostArgs, home: str) -> CmdResult:
    if args.host_cmd == "add":
        return add_host(args, home)
    if args.host_cmd == "list":
        return host_list(home)
    if args.host_cmd == "remove":
        return host_remove(args.name, args.yes, home)
    if args.host_cmd in ("enable", "disable"):
        return host_set_enabled(args.name, args.host_cmd == "enable", home)
    if args.host_cmd == "ping":
        return host_ping(args.name or None, home)
    raise TaskmgrError("bad_args", "unknown host subcommand %r" % args.host_cmd)


# ---------------------------------------------------------------------------
# list


def host_list(home: str) -> CmdResult:
    servers = load_machines(home)
    return {
        "machines": os.path.join(home, "machines.json"),
        "servers": [
            {
                "name": s["name"],
                "enabled": s.get("enabled", True),
                "transport": s.get("transport", "ssh"),
                "destination": destination(s),
                "remote_root": s["remote_root"],
            }
            for s in servers
        ],
    }, 0


# ---------------------------------------------------------------------------
# remove


def _find(servers: list[Server], name: str) -> Server:
    for s in servers:
        if s["name"] == name:
            return s
    raise TaskmgrError("unknown_host", "host %r is not in the machine list" % name, host=name)


def _is(s: JsonValue, name: str) -> bool:
    return isinstance(s, dict) and s.get("name") == name


def _must_have(servers: list[JsonValue], name: str) -> None:
    """The entry may have been removed since load_machines read the file."""
    if not any(_is(s, name) for s in servers):
        raise TaskmgrError("unknown_host", "host %r is not in the machine list" % name, host=name)


def host_set_enabled(name: str, enabled: bool, home: str) -> CmdResult:
    """`task host enable|disable NAME`: set `enabled` in the machine entry (local only)."""
    path = os.path.join(home, "machines.json")
    changed = _find(load_machines(home), name).get("enabled", True) != enabled

    def set_flag(servers: list[JsonValue]) -> None:
        _must_have(servers, name)
        for s in servers:
            if isinstance(s, dict) and s.get("name") == name:
                s["enabled"] = enabled

    if changed:
        edit_machines(home, set_flag)
    return {"host": name, "enabled": enabled, "changed": changed, "machines": path}, 0


def host_remove(name: str, yes: bool, home: str) -> CmdResult:
    path = os.path.join(home, "machines.json")
    entry = _find(load_machines(home), name)
    ui = Ui(yes)
    ui.say(
        "task host remove %s: %s, remote_root %s"
        % (name, destination(entry), entry["remote_root"]),
        BOLD,
    )
    try:
        ui.confirm("remove %s from %s (the server itself is not touched)" % (name, path))
    except NonInteractive as e:
        raise TaskmgrError("not_interactive", str(e)) from e
    except Declined:
        return {"host": name, "removed": False, "machines": path}, 2

    def drop(servers: list[JsonValue]) -> None:
        _must_have(servers, name)
        servers[:] = [s for s in servers if not _is(s, name)]

    edit_machines(home, drop)
    return {
        "host": name,
        "removed": True,
        "machines": path,
        "entry": cast(Mapping[str, object], cast(object, entry)),
    }, 0


# ---------------------------------------------------------------------------
# ping


def _ping_one(server: Server) -> dict[str, object]:
    row: dict[str, object] = {"host": server["name"], "enabled": server.get("enabled", True)}
    start = time.monotonic()
    try:
        reply = Remote(server).call(["version"], timeout=PING_TIMEOUT)
    except TaskmgrError as e:
        row.update(
            {
                "reachable": False,
                "ms": round((time.monotonic() - start) * 1000),
                "code": e.code,
                "error": e.message,
            }
        )
        return row
    row["reachable"] = True
    row["ms"] = round((time.monotonic() - start) * 1000)
    for k in ("protocol_version", "features", "code_version", "code_dir", "user", "hostname"):
        row[k] = reply.get(k)
    return row


def host_ping(name: str | None, home: str) -> CmdResult:
    servers = load_machines(home)
    skipped: list[str] = []
    if name is not None:
        servers = [_find(servers, name)]
    else:
        skipped = [s["name"] for s in servers if not s.get("enabled", True)]
        servers = [s for s in servers if s.get("enabled", True)]
    rows: list[dict[str, object]] = []
    if servers:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(servers))) as ex:
            rows = list(ex.map(_ping_one, servers))
    ok = all(r["reachable"] for r in rows)
    return {"hosts": rows, "skipped": skipped, "complete": ok}, (0 if ok else 2)


# ---------------------------------------------------------------------------
# text output


def _s(v: object, default: str = "") -> str:
    return cell(v, default)


def render(args: HostArgs, out: Mapping[str, object], color: bool) -> str:
    if args.host_cmd == "add":
        return text_add(out, color)
    if args.host_cmd == "list":
        return text_list(out, color)
    if args.host_cmd == "remove":
        return text_remove(out, color)
    if args.host_cmd in ("enable", "disable"):
        return text_enabled(out, color)
    return text_ping(out, color)


def text_enabled(out: Mapping[str, object], color: bool) -> str:
    host, on = out.get("host"), bool(out.get("enabled"))
    state = "enabled" if on else "disabled"
    if not out.get("changed"):
        return message("unchanged", YELLOW, "%s is already %s" % (host, state), color)
    if on:
        return message("enabled", GREEN, "%s is used for submits and queries again" % host, color)
    return message(
        "disabled",
        YELLOW,
        "%s is left out of round-robin submits and of queries over every server" % host,
        color,
    ) + hint(
        "`--host %s` still reaches it; `task host ping %s` tells when it answers again; "
        % (host, host)
        + "`task host enable %s` turns it back on" % host,
        color,
    )


def text_add(out: Mapping[str, object], color: bool) -> str:
    steps = _rows(out, "steps")
    rows: Sequence[Sequence[str]] = [
        [_s(s.get("name")), _s(s.get("status")), _s(s.get("detail"))] for s in steps
    ]
    text = render_table(("STEP", "STATUS", "DETAIL"), rows, color, {1: STEP_COLORS})
    host = out.get("host")
    failed = any(s.get("status") in ("failed", "declined") for s in steps)
    if out.get("plan"):
        planned = [str(n) for n in cast(list[object], out.get("planned") or [])]
        if failed:
            return text + message("plan", RED, "a check failed on host %s" % host, color)
        if not planned:
            return text + message("plan", GREEN, "nothing to change on host %s" % host, color)
        return (
            text
            + message(
                "plan",
                YELLOW,
                "host %s: steps %s would change; nothing was changed" % (host, ", ".join(planned)),
                color,
            )
            + hint("run it without --plan to make the changes", color)
        )
    if any(s.get("status") == "kept" for s in steps):
        text += hint(
            "old code was kept; `task host add %s --prune-old-code` deletes it" % host, color
        )
    if out.get("complete"):
        return text + message("done", GREEN, "host %s is set up" % out.get("host"), color)
    return text + message(
        "incomplete",
        RED,
        "host %s is NOT set up; fix the failed or declined step and run it again" % out.get("host"),
        color,
    )


def text_list(out: Mapping[str, object], color: bool) -> str:
    servers = _rows(out, "servers")
    if not servers:
        return message("empty", YELLOW, "no servers in %s" % out.get("machines"), color) + hint(
            "`task host add NAME --ssh USER@HOST` adds one", color
        )
    rows = [
        [
            _s(s.get("name")),
            "yes" if s.get("enabled") else "no",
            _s(s.get("destination")),
            _s(s.get("remote_root")),
        ]
        for s in servers
    ]
    return render_table(
        ("NAME", "ENABLED", "DESTINATION", "REMOTE_ROOT"),
        rows,
        color,
        {1: {"yes": GREEN, "no": YELLOW}},
    )


def text_remove(out: Mapping[str, object], color: bool) -> str:
    host, machines = out.get("host"), out.get("machines")
    if not out.get("removed"):
        return message("declined", YELLOW, "%s stays in %s" % (host, machines), color)
    return message("removed", GREEN, "%s from %s" % (host, machines), color) + hint(
        "the server itself (code, service, remote_root and its jobs) is untouched; "
        + "`task host add %s` adds it again" % host,
        color,
    )


def text_ping(out: Mapping[str, object], color: bool) -> str:
    hosts = _rows(out, "hosts")
    raw = out.get("skipped")
    skipped = [str(n) for n in cast(list[object], raw)] if isinstance(raw, list) else []
    note = (
        message("skipped", YELLOW, "disabled, not pinged: %s" % ", ".join(skipped), color)
        + hint("`task host ping NAME` pings a disabled server", color)
        if skipped
        else ""
    )
    if not hosts:
        if skipped:
            return note
        return message("empty", YELLOW, "no servers in the machine list", color)
    rows = [
        [
            _s(h.get("host")),
            "ok" if h.get("reachable") else "unreachable",
            "%sms" % _s(h.get("ms")) if h.get("reachable") else "-",
            _s(h.get("protocol_version"), "-"),
            _s(h.get("code_version"), "-" if not h.get("reachable") else "none")[:12],
            _s(h.get("user"), "-"),
            _s(h.get("hostname"), "-"),
        ]
        for h in hosts
    ]
    text = render_table(
        ("HOST", "STATUS", "RTT", "PROTOCOL", "CODE_VERSION", "USER", "HOSTNAME"),
        rows,
        color,
        {1: {"ok": GREEN, "unreachable": RED}},
    )
    for h in hosts:
        if not h.get("reachable"):
            text += message("unreachable", RED, "%s: %s" % (h.get("host"), h.get("error")), color)
    return text + note
