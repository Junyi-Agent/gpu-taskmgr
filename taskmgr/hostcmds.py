"""The host commands of `task`: set up servers and manage the machine list
($TASKMGR_HOME/machines.json).

    task create host NAME           set up a server (hostsetup) and add it to the list
    task update host NAME           update a listed server's code (hostsetup)
    task get hosts [NAME]           the configured servers
    task delete host NAME           remove a server from the list
    task enable|disable host NAME   use a server for submits and queries, or not
    task ping host [NAME]           check that servers answer

Interface used by taskmgr.py's command table: args_*() add a command's arguments and return
its description; the command functions return (JSON object, exit code); text_*() render that
object as the colored text printed with `-o text` (the default).
"""

import argparse
import concurrent.futures
import os
import time
from collections.abc import Mapping, Sequence
from typing import cast

from .common import TaskmgrError
from .hostsetup import STEP_COLORS, AddHostArgs, add_host, destination, update_host
from .prompt import Declined, NonInteractive, Ui
from .records import JsonValue, Server
from .textout import BOLD, GREEN, RED, YELLOW, CmdResult, cell, hint, message, render_table
from .textout import rows as _rows
from .transport import (
    Remote,
    edit_machines,
    find_server,
    is_entry,
    load_machines,
    unknown_host,
)

PING_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Arguments (each returns the command's description)

PYZ_HELP = (
    "taskmgr-remote.pyz to upload, built by `task build pyz` (default: "
    + "dist/taskmgr-remote.pyz in the checkout, or next to task.pyz); its code version "
    + "must equal this task's"
)
NAME_HELP = "name in the machine list"


def _setup_options(p: argparse.ArgumentParser, prune_help: str) -> None:
    """The options `create host` and `update host` share."""
    _ = p.add_argument("--pyz", metavar="FILE", help=PYZ_HELP)
    _ = p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="answer yes to every prompt, except deletions (see --prune-old-code)",
    )
    _ = p.add_argument(
        "--plan",
        "--dry-run",
        action="store_true",
        help="check everything and print every change it would make; change nothing",
    )
    _ = p.add_argument("--prune-old-code", action="store_true", help=prune_help)


def args_create(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", help=NAME_HELP)
    _ = p.add_argument("--ssh", metavar="USER@HOST", help="ssh destination (required when new)")
    _ = p.add_argument("--port", type=int, help="ssh port (default 22)")
    _ = p.add_argument("--identity-file", metavar="FILE", help="ssh private key file")
    _ = p.add_argument(
        "--ssh-option",
        action="append",
        metavar="KEY=VALUE",
        help="extra `ssh -o` option (repeatable); replaces an option with the same key",
    )
    g = p.add_mutually_exclusive_group()
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
    _setup_options(
        p,
        "delete the code older versions installed outside remote_root (~/taskmgr, "
        + "~/taskmgr.old-*, ~/taskmgr-remote.pyz); never done by --yes alone",
    )
    return (
        "Set up a server, or bring a listed one to the wanted state: check the server step by "
        + "step and change only what differs (code, remote_root, systemd user service, "
        + "linger, machine list). Every change is confirmed with [y/N]; a second run changes "
        + "nothing. --yes answers yes to every change except deleting code of older versions "
        + "outside remote_root (step old_code): that needs --prune-old-code or a y typed in a "
        + "terminal, otherwise it is kept. --plan runs every check and prints what would "
        + "change, changing nothing (exit 0: nothing to change, 1: changes planned, 2: a check "
        + "failed). The server's storage root is $TASKMGR_REMOTE_ROOT (default: the existing "
        + "entry's, else ~/.taskmgr-remote on the server); it holds config.json, the job data "
        + "and the code."
    )


def args_update(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", help=NAME_HELP)
    _setup_options(p, "also delete the code older versions installed outside remote_root")
    p.set_defaults(ssh=None, port=None, identity_file=None, ssh_option=None, enabled=None)
    return (
        "Update the taskmgr-remote code of a server already in the machine list to the code "
        + "version of this task: upload the matching taskmgr-remote.pyz, check the job "
        + "directories, restart the service on the new code and check that it answers. The "
        + "same steps as `task create host NAME`, with the machine entry as it is (to change "
        + "its ssh settings, use `task create host`). Running jobs keep running "
        + "(KillMode=process). Every change is confirmed with [y/N] (--yes answers them); on "
        + "an up-to-date server nothing changes and nothing is asked. A disabled server can be "
        + "updated. --plan prints what would change (exit 0: nothing, 1: changes planned, "
        + "2: a check failed). Build the archive first with `task build pyz`."
    )


def args_get(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", nargs="?", default="", help="one server (default: all)")
    return "List the servers of the machine list (or one): name, enabled, destination, root."


def args_delete(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", help=NAME_HELP)
    _ = p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    return (
        "Remove a server from the local machine list. The server itself (code, service, "
        + "remote_root and its jobs) is not touched; `task create host` adds it again."
    )


def args_enable(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", help=NAME_HELP)
    return (
        "Mark a server enabled in the machine list: round-robin submits and the queries over "
        + "every server (get jobs, the jobid lookup, whoami, version) use it again."
    )


def args_disable(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", help=NAME_HELP)
    return (
        "Mark a server disabled in the machine list, e.g. while it is down: round-robin "
        + "submits skip it and the queries over every server (get jobs, the jobid lookup, "
        + "whoami, version, ping host) do not wait for it. `--host NAME` and "
        + "`task ping host NAME` still reach it. Nothing changes on the server; its jobs keep "
        + "running."
    )


def args_ping(p: argparse.ArgumentParser) -> str:
    _ = p.add_argument("name", nargs="?", default="", help="one server (default: all enabled)")
    return (
        "Run `taskmgr-remote version` on each enabled server (in parallel), or on the named "
        + "one even if it is disabled, and report whether it answers, the round-trip time and "
        + "the installed code version. Exit 0 when every pinged server answered, 2 otherwise."
    )


# ---------------------------------------------------------------------------
# Commands


def create(args: AddHostArgs, home: str) -> CmdResult:
    return add_host(args, home)


def update(args: AddHostArgs, home: str) -> CmdResult:
    return update_host(args, home)


# ---------------------------------------------------------------------------
# list


def host_list(name: str | None, home: str) -> CmdResult:
    """The machine list, or the entry of one server."""
    servers = load_machines(home)
    if name is not None:
        servers = [find_server(servers, name)]

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


def _must_have(servers: list[JsonValue], name: str) -> None:
    """The entry may have been removed since load_machines read the file."""
    if not any(is_entry(s, name) for s in servers):
        raise unknown_host(name)


def host_set_enabled(name: str, enabled: bool, home: str) -> CmdResult:
    """`task enable|disable host NAME`: set `enabled` in the machine entry (local only)."""
    path = os.path.join(home, "machines.json")
    changed = find_server(load_machines(home), name).get("enabled", True) != enabled

    def set_flag(servers: list[JsonValue]) -> None:
        _must_have(servers, name)
        for s in servers:
            if is_entry(s, name):
                s["enabled"] = enabled

    if changed:
        edit_machines(home, set_flag)
    return {"host": name, "enabled": enabled, "changed": changed, "machines": path}, 0


def host_remove(name: str, yes: bool, home: str) -> CmdResult:
    path = os.path.join(home, "machines.json")
    entry = find_server(load_machines(home), name)
    ui = Ui(yes)
    ui.say(
        "task delete host %s: %s, remote_root %s"
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
        servers[:] = [s for s in servers if not is_entry(s, name)]

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
        servers = [find_server(servers, name)]
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


def text_create(_args: object, out: Mapping[str, object], color: bool) -> str:
    return text_setup(out, color, "create")


def text_update(_args: object, out: Mapping[str, object], color: bool) -> str:
    return text_setup(out, color, "update")


def text_enabled(_args: object, out: Mapping[str, object], color: bool) -> str:
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
        "`--host %s` still reaches it; `task ping host %s` tells when it answers again; "
        % (host, host)
        + "`task enable host %s` turns it back on" % host,
        color,
    )


def text_setup(out: Mapping[str, object], color: bool, command: str) -> str:
    """The step table of `task create host` or `task update host` (command: the verb)."""
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
            "old code was kept; `task %s host %s --prune-old-code` deletes it" % (command, host),
            color,
        )
    if out.get("complete"):
        return text + message("done", GREEN, "host %s is set up" % out.get("host"), color)
    return text + message(
        "incomplete",
        RED,
        "host %s is NOT set up; fix the failed or declined step and run it again" % out.get("host"),
        color,
    )


def text_list(_args: object, out: Mapping[str, object], color: bool) -> str:
    servers = _rows(out, "servers")
    if not servers:
        return message("empty", YELLOW, "no servers in %s" % out.get("machines"), color) + hint(
            "`task create host NAME --ssh USER@HOST` adds one", color
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


def text_delete(_args: object, out: Mapping[str, object], color: bool) -> str:
    host, machines = out.get("host"), out.get("machines")
    if not out.get("removed"):
        return message("declined", YELLOW, "%s stays in %s" % (host, machines), color)
    return message("removed", GREEN, "%s from %s" % (host, machines), color) + hint(
        "the server itself (code, service, remote_root and its jobs) is untouched; "
        + "`task create host %s` adds it again" % host,
        color,
    )


def text_ping(_args: object, out: Mapping[str, object], color: bool) -> str:
    hosts = _rows(out, "hosts")
    raw = out.get("skipped")
    skipped = [str(n) for n in cast(list[object], raw)] if isinstance(raw, list) else []
    note = (
        message("skipped", YELLOW, "disabled, not pinged: %s" % ", ".join(skipped), color)
        + hint("`task ping host NAME` pings a disabled server", color)
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
