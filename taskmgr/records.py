"""Static types for the JSON records taskmgr reads and writes.

Python 3.10 has no `NotRequired`, so each record with optional keys is a required base
class plus a `total=False` subclass. Values parsed from JSON are `JsonValue` until they
are checked or cast to one of the records below.
"""

from typing import TYPE_CHECKING, TypeAlias, TypedDict

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict: TypeAlias = dict[str, JsonValue]

if TYPE_CHECKING:
    from typing_extensions import override
else:  # runtime stays stdlib-only; typing.override only exists from Python 3.12

    def override(func):
        return func


__all__ = [
    "AttemptEntry",
    "ErrorInfo",
    "GpuDecl",
    "Job",
    "JsonDict",
    "JsonValue",
    "KeyMapping",
    "LocalInjection",
    "Problem",
    "RemoteConfig",
    "Runner",
    "Server",
    "SkippedEntry",
    "SshConfig",
    "TaskEntry",
    "TaskEnvironment",
    "TaskResultSpec",
    "TaskSpec",
    "TaskmgrMeta",
    "TakeoverNote",
    "UnknownNote",
    "override",
]


class GpuDecl(TypedDict):
    """One resolved GPU declaration in job.json / _taskmgr.gpus."""

    declared: int | str
    uuid: str


class _ErrorBase(TypedDict):
    kind: str
    message: str


class ErrorInfo(_ErrorBase, total=False):
    """`error` in job.json, runner.json and _taskmgr."""

    details: JsonValue


class _AttemptEntryBase(TypedDict):
    attempt: int
    status: str


class AttemptEntry(_AttemptEntryBase, total=False):
    """An item of job.json `attempts`."""

    dispatched_at: str
    ended_at: str | None


class _JobBase(TypedDict):
    schema_version: int
    jobid: str
    task_id: str
    state: str
    submitted_at: str
    updated_at: str
    enqueue_seq: int
    content_hash: str
    submission_key: str
    gpus: list[GpuDecl]
    cancel_requested: bool
    auto_restarts: int
    attempts: list[AttemptEntry]
    current_attempt: int | None
    stop_reason: str | None
    error: ErrorInfo | None
    retries: int


class Job(_JobBase, total=False):
    """job.json."""

    ended_at: str | None


class TakeoverNote(TypedDict):
    at: str
    by_pid: int


class UnknownNote(TypedDict):
    kind: str
    message: str
    details: JsonValue
    at: str


class _TaskmgrMetaBase(TypedDict):
    jobid: str
    task_id: str
    attempt: int | None
    gpus: list[GpuDecl]
    status: str
    return_code: int | None
    signal: int | None
    validation_errors: list[str]
    error: ErrorInfo | None
    warnings: list[str]
    final: bool


class TaskmgrMeta(_TaskmgrMetaBase, total=False):
    """The `_taskmgr` block of result.json (also kept as runner.json `taskmgr`)."""

    host: str
    started_at: str | None
    ended_at: str | None
    paths: dict[str, str]
    note: str


class _RunnerBase(TypedDict):
    attempt: int


class Runner(_RunnerBase, total=False):
    """runner.json: written step by step, so every key except `attempt` is optional."""

    schema_version: int
    jobid: str
    status: str
    gpus: list[GpuDecl]
    dispatched_at: str
    final: bool
    exited: bool
    shim_pid: int
    shim_start_time: int | None
    boot_id: str
    pgid: int
    leader_pid: int
    leader_start_time: int | None
    argv_hash: str
    argv: list[str]
    started_at: str
    started_at_epoch: float
    return_code: int | None
    signal: int | None
    ended_at: str
    error: ErrorInfo
    warnings: list[str]
    taskmgr: TaskmgrMeta
    finalized_at: str
    takeovers: list[TakeoverNote]
    stop_unconfirmed_reason: str | None
    interrupted_reason: str
    unknown: UnknownNote


class TaskEntry(TypedDict):
    argv: list[str]


class TaskEnvironment(TypedDict, total=False):
    venv: str
    required_paths: list[str]


class TaskResultSpec(TypedDict, total=False):
    required_outputs: list[str]


class _TaskSpecBase(TypedDict):
    schema_version: int
    task_id: str
    entry: TaskEntry
    gpus: list[int | str]


class TaskSpec(_TaskSpecBase, total=False):
    """task.json after validation (Section 2.2 of the spec)."""

    environment: TaskEnvironment
    env: dict[str, str]
    timeout_seconds: int | None
    result: TaskResultSpec


class _ProblemBase(TypedDict):
    code: str
    message: str


class Problem(_ProblemBase, total=False):
    """One package validation error (`taskmgr validate` errors, invalid_package details)."""

    path: str


class SkippedEntry(TypedDict):
    """An archive entry safe_extract skipped as unsafe (skip_unsafe=True)."""

    path: str
    reason: str


class KeyMapping(TypedDict):
    """submission-keys/<key>.json."""

    submission_key: str
    jobid: str
    content_hash: str
    created_at: str


class RemoteConfig(TypedDict):
    """<remote_root>/config.json merged over the defaults."""

    max_package_bytes: int | None
    max_cpu_jobs: int | None
    max_auto_restarts: int
    stop_grace_seconds: float
    kill_confirm_seconds: float
    control_poll_seconds: float
    scheduler_poll_seconds: float
    base_env: dict[str, str]


class SshConfig(TypedDict, total=False):
    host: str
    user: str
    port: int
    identity_file: str
    options: list[str]


class LocalInjection(TypedDict, total=False):
    """Test-only connection-failure injection for transport "local"."""

    fail: str
    fail_if_exists: str


class _ServerBase(TypedDict):
    name: str
    remote_root: str
    remote_command: str


class Server(_ServerBase, total=False):
    """One entry of the local machines.json."""

    enabled: bool
    transport: str
    ssh: SshConfig
    local: LocalInjection
