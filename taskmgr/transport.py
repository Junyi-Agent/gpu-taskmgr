"""Machine list and transports: run taskmgr-remote subcommands and shell scripts on a
server over ssh (or locally for the test-only transport "local")."""

import concurrent.futures
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from typing import IO, TypedDict, cast

from .common import PROTOCOL_VERSION, TaskmgrError, atomic_write_bytes, flocked, read_json
from .records import JsonDict, JsonValue, LocalInjection, Server, SshConfig


class HostError(TypedDict):
    """One unreachable server in a multi-server query."""

    host: str
    error: str
    code: str


# ssh gives up connecting after this many seconds (a machine entry's own
# `-o ConnectTimeout=N` wins).
CONNECT_TIMEOUT = 10
# ssh drops a connection whose server stopped answering after about
# SERVER_ALIVE_INTERVAL * SERVER_ALIVE_COUNT_MAX seconds (a machine entry's own
# `-o ServerAliveInterval=N` / `-o ServerAliveCountMax=N` wins).
SERVER_ALIVE_INTERVAL = 15
SERVER_ALIVE_COUNT_MAX = 3
# A JSON subcommand without an upload (jobs, status, cancel, retry, resolve, version...)
# waits at most this long for each server; TASKMGR_QUERY_TIMEOUT overrides it.
QUERY_TIMEOUT = 30.0
# stderr lines by which ssh (not the remote command) reports a failed connection.
SSH_FAILURE_MARKERS = (
    "ssh:",
    "connection closed by",
    "connection reset by",
    "connection refused",
    "connection timed out",
    "could not resolve hostname",
    "host key verification failed",
    "permission denied (",
    "kex_exchange_identification",
    "timeout, server",
    "client_loop:",
    "broken pipe",
)


def query_timeout() -> float:
    try:
        return float(os.environ.get("TASKMGR_QUERY_TIMEOUT", QUERY_TIMEOUT))
    except ValueError:
        return QUERY_TIMEOUT


def ssh_error(stderr: str) -> str:
    """The message of a failed ssh connection: its stderr, prefixed with "ssh: " once."""
    err = stderr.strip() or "connection failed"
    return err if err.startswith("ssh: ") else "ssh: " + err


def looks_like_ssh_failure(stderr: str) -> bool:
    low = stderr.lower()
    return any(m in low for m in SSH_FAILURE_MARKERS)


class ConnError(TaskmgrError):
    """The server could not be reached, or did not answer in time: the outcome of the
    command is unknown."""

    def __init__(
        self, message: str, host: str | None = None, code: str = "connection_error"
    ) -> None:
        super().__init__(code, message, 2, host)


class NoReplyError(ConnError):
    """The server was reached but the remote command gave no JSON reply (it crashed):
    as with a lost connection, whether it did its work is unknown."""

    def __init__(self, message: str, host: str | None = None) -> None:
        super().__init__(message, host, "remote_error")


def json_obj(v: JsonValue) -> JsonDict:
    """A JSON value the remote protocol guarantees to be an object."""
    if not isinstance(v, dict):
        raise TypeError("expected a JSON object in the remote reply, got %r" % (v,))
    return v


def json_str(v: JsonValue, default: str) -> str:
    return v if isinstance(v, str) else default


def remote_error(reply: JsonDict, host: str, with_details: bool) -> TaskmgrError:
    """Turn a remote {"error": {...}} reply into a TaskmgrError."""
    raw = reply["error"]
    if not isinstance(raw, dict):
        return TaskmgrError("remote_error", "malformed error reply: %r" % (raw,), 2, host)
    e = raw
    details = e.get("details") if with_details else None
    return TaskmgrError(
        json_str(e.get("code", "remote_error"), "remote_error"),
        json_str(e.get("message", ""), ""),
        2,
        host,
        details,
    )


def load_machines(home: str) -> list[Server]:
    path = os.path.join(home, "machines.json")
    try:
        data = read_json(path)
    except ValueError as e:
        raise TaskmgrError("bad_machines", "%s is not valid JSON: %s" % (path, e)) from e
    except OSError as e:
        raise TaskmgrError("bad_machines", "cannot read %s: %s" % (path, e)) from e
    if data is None:
        raise TaskmgrError(
            "no_machines",
            "machine list %s does not exist (see taskmgr/deploy/machines.example.json)" % path,
        )
    servers = data.get("servers") if isinstance(data, dict) else data
    if not isinstance(servers, list):
        raise TaskmgrError("bad_machines", '%s must contain a "servers" array' % path)
    names: set[object] = set()
    for entry in servers:
        s = entry if isinstance(entry, dict) else None
        if s is None or not isinstance(s.get("name"), str) or not s["name"] or s["name"] in names:
            raise TaskmgrError("bad_machines", "every server needs a unique name: %r" % (entry,))
        names.add(s["name"])
        if s.get("transport", "ssh") not in ("ssh", "local"):
            raise TaskmgrError(
                "bad_machines", "server %s: transport must be ssh or local" % s["name"]
            )
        if not s.get("remote_root") or not s.get("remote_command"):
            raise TaskmgrError(
                "bad_machines", "server %s needs remote_root and remote_command" % s["name"]
            )
    # Checked above: every entry is an object with name, remote_root and remote_command.
    return cast(list[Server], servers)


class Remote:
    def __init__(self, server: Server) -> None:
        self.server: Server = server
        self.name: str = server["name"]

    def argv(self, args: Sequence[object]) -> list[str]:
        s = self.server
        base = shlex.split(s["remote_command"]) + [
            "--root",
            s["remote_root"],
            "--protocol-version",
            str(PROTOCOL_VERSION),
        ]
        base += [str(a) for a in args]
        if s.get("transport", "ssh") == "local":
            return base
        return self.ssh_argv() + ["--", shlex.join(base)]

    def ssh_argv(self) -> list[str]:
        """`ssh <options> <destination>`, without the remote command."""
        ssh: SshConfig = self.server.get("ssh") or {}
        cmd = ["ssh", "-o", "BatchMode=yes"]
        port = ssh.get("port")
        if port:
            cmd += ["-p", str(port)]
        identity_file = ssh.get("identity_file")
        if identity_file:
            cmd += ["-i", os.path.expanduser(identity_file)]
        options = list(ssh.get("options") or [])
        cmd += options
        for name, value in (
            ("ConnectTimeout", CONNECT_TIMEOUT),
            ("ServerAliveInterval", SERVER_ALIVE_INTERVAL),
            ("ServerAliveCountMax", SERVER_ALIVE_COUNT_MAX),
        ):
            if not any(name.lower() in o.lower() for o in options):
                cmd += ["-o", "%s=%d" % (name, value)]
        host = ssh.get("host") or self.name
        user = ssh.get("user")
        cmd += ["%s@%s" % (user, host) if user else host]
        return cmd

    def shell_argv(self, script: str) -> list[str]:
        """Run `sh -c script` on the server (locally for transport "local")."""
        cmd = ["sh", "-c", script]
        if self.server.get("transport", "ssh") == "local":
            return cmd
        return self.ssh_argv() + ["--", shlex.join(cmd)]

    def _inject(self) -> None:
        # Test-only failure injection for transport "local" (Section 18.1).
        if self.server.get("transport") != "local":
            return
        inj: LocalInjection = self.server.get("local") or {}
        fail = inj.get("fail")
        if fail:
            raise ConnError(str(fail), self.name)
        f = inj.get("fail_if_exists")
        if f and os.path.exists(f):
            raise ConnError("injected connection failure (%s exists)" % f, self.name)

    def _env(self) -> dict[str, str] | None:
        """Environment for the transport process: None (inherit), except for a test-only
        `"local": {"home": DIR}`, which sets HOME so a local "server" has its own home."""
        if self.server.get("transport") != "local":
            return None
        inj: LocalInjection = self.server.get("local") or {}
        home = inj.get("home")
        if not home:
            return None
        env = dict(os.environ)
        env["HOME"] = home
        return env

    def shell(
        self, script: str, stdin: bytes | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        """Run a shell script on the server: (exit code, stdout, stderr).

        Raises ConnError when the server cannot be reached or on timeout. Exit code 255
        is ssh's for a failed connection, but a script may exit 255 too: it counts as a
        connection failure only when stderr reads like ssh's.
        """
        self._inject()
        try:
            p = subprocess.run(
                self.shell_argv(script),
                input=stdin or b"",
                capture_output=True,
                env=self._env(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ConnError("remote command timed out", self.name) from None
        except OSError as e:
            raise ConnError("cannot run transport: %s" % e, self.name) from e
        out = p.stdout.decode(errors="replace")
        err = p.stderr.decode(errors="replace")
        if self._is_conn_failure(p.returncode) and looks_like_ssh_failure(err):
            raise ConnError(ssh_error(err), self.name)
        return p.returncode, out, err

    def _is_conn_failure(self, rc: int) -> bool:
        return self.server.get("transport", "ssh") == "ssh" and rc == 255

    def call(
        self,
        args: Sequence[object],
        stdin_path: str | None = None,
        timeout: float | None = None,
    ) -> JsonDict:
        """Run a JSON subcommand and return its object; raise TaskmgrError/ConnError.

        timeout None: no limit for an upload (stdin_path), else query_timeout().
        """
        self._inject()
        if timeout is None and stdin_path is None:
            timeout = query_timeout()
        upload = open(stdin_path, "rb") if stdin_path else None
        try:
            p = subprocess.run(
                self.argv(args),
                stdin=upload if upload is not None else subprocess.DEVNULL,
                capture_output=True,
                env=self._env(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ConnError("no answer within %gs" % timeout, self.name) from None
        except OSError as e:
            raise ConnError("cannot run transport: %s" % e, self.name) from e
        finally:
            if upload is not None:
                upload.close()
        err = p.stderr.decode(errors="replace").strip()
        if self._is_conn_failure(p.returncode):
            raise ConnError(ssh_error(err), self.name)
        obj: JsonValue = None
        for line in reversed(p.stdout.decode(errors="replace").splitlines()):
            if line.strip():
                try:
                    obj = cast(JsonValue, json.loads(line))
                except ValueError:
                    pass
                break
        if not isinstance(obj, dict):
            raise NoReplyError(
                "no valid JSON from remote (exit %d): %s" % (p.returncode, err[-2000:]), self.name
            )
        reply = obj
        check_protocol(reply, self.name)
        if "error" in reply:
            raise remote_error(reply, self.name, with_details=True)
        return reply

    def stream(self, args: Sequence[object], stdout: IO[bytes]) -> int:
        """Run a streaming subcommand (logs/fetch) writing to stdout; returns exit code."""
        self._inject()
        try:
            p = subprocess.run(
                self.argv(args),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.PIPE,
                env=self._env(),
            )
        except OSError as e:
            raise ConnError("cannot run transport: %s" % e, self.name) from e
        err = p.stderr.decode(errors="replace").strip()
        if self._is_conn_failure(p.returncode):
            raise ConnError(ssh_error(err), self.name)
        if p.returncode != 0:
            obj: JsonValue = None
            for line in reversed(err.splitlines()):
                try:
                    obj = cast(JsonValue, json.loads(line))
                    break
                except ValueError:
                    continue
            if isinstance(obj, dict) and "error" in obj:
                check_protocol(obj, self.name)
                raise remote_error(obj, self.name, with_details=False)
            raise TaskmgrError(
                "remote_error", "remote exited %d: %s" % (p.returncode, err[-2000:]), 2, self.name
            )
        return 0


def reply_features(reply: JsonDict) -> list[str]:
    """The `features` a remote reply reports; [] for a server older than the list."""
    raw = reply.get("features")
    return [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []


def require_features(reply: JsonDict, needed: Sequence[str], host: str, what: str) -> None:
    """Raise unsupported_feature when the server that sent `reply` lacks one of `needed`."""
    have = reply_features(reply)
    missing = [f for f in needed if f not in have]
    if not missing:
        return
    code = reply.get("code_version")
    runs = "code %s" % code[:12] if isinstance(code, str) and code else "older code"
    raise TaskmgrError(
        "unsupported_feature",
        "server %s runs %s without %s support, needed by %s; run `task host add %s` to update it"
        % (host, runs, ", ".join(missing), what, host),
        2,
        host,
        {"missing": missing, "server_features": have},
    )


def check_protocol(obj: JsonDict, host: str | None) -> None:
    v = obj.get("protocol_version")
    if v != PROTOCOL_VERSION:
        raise TaskmgrError(
            "protocol_mismatch",
            "remote protocol_version %r is incompatible with local %d" % (v, PROTOCOL_VERSION),
            2,
            host,
        )


def query_all(
    servers: list[Server], args: Sequence[object], timeout: float | None = None
) -> tuple[dict[str, JsonDict], list[HostError]]:
    """Call a JSON subcommand on every server in parallel: ({name: obj}, [errors]).

    timeout: per server, default query_timeout()."""
    results: dict[str, JsonDict] = {}
    errors: list[HostError] = []
    if not servers:
        return results, errors
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(servers))) as ex:
        futs: dict[concurrent.futures.Future[JsonDict], str] = {
            ex.submit(
                Remote(s).call, args, None, query_timeout() if timeout is None else timeout
            ): s["name"]
            for s in servers
        }
        for f in concurrent.futures.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
            except TaskmgrError as e:
                errors.append({"host": name, "error": e.message, "code": e.code})
    order: list[str] = [s["name"] for s in servers]
    errors.sort(key=lambda e: order.index(e["host"]))
    return results, errors


def edit_machines(home: str, edit: Callable[[list[JsonValue]], None]) -> None:
    """Change the "servers" array of $TASKMGR_HOME/machines.json (an empty list when the
    file does not exist) under machines.lock, then write the file back atomically. `edit`
    changes the list in place; it may raise to leave the file as it is."""
    path = os.path.join(home, "machines.json")
    os.makedirs(home, exist_ok=True)
    with flocked(os.path.join(home, "machines.lock")):
        try:
            data = read_json(path)
        except ValueError as e:
            raise TaskmgrError("bad_machines", "%s is not valid JSON: %s" % (path, e)) from e
        if data is None:
            empty: JsonDict = {"servers": []}
            data = empty
        servers = data.get("servers") if isinstance(data, dict) else data
        if not isinstance(servers, list):
            raise TaskmgrError("bad_machines", '%s must contain a "servers" array' % path)
        edit(servers)
        atomic_write_bytes(path, (json.dumps(data, indent=2) + "\n").encode())
