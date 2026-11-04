"""Text renderers of the `task` commands: (args, JSON result, color) -> text for stdout.

The default output; `--detail` prints the JSON object instead. Renderers only build text:
warnings that go with a result (skipped or unreachable servers) are printed by the
command layer (taskmgr.py), in both modes.
"""

import shlex
from collections.abc import Mapping
from typing import cast

from . import hostcmds
from .cliparse import Args
from .common import PHASE_GPU, PHASE_PRECOMPILE
from .records import JsonDict
from .textout import (
    BOLD,
    GREEN,
    PENDING_GPU_STATE,
    PRECOMPILING_STATE,
    RED,
    STATE_COLORS,
    YELLOW,
    as_dict,
    hint,
    message,
    paint,
    render_fields,
    render_table,
    rows,
)
from .textout import cell as _s
from .transport import json_str as _str

JOBS_COLUMNS = ("JOBID", "TASK_ID", "SUBMITTED", "HOST", "STATE")
RETRY_COLUMNS = ("JOBID", "RETRY_OF", "HOST", "STATE")

Fields = list[tuple[str, str, str | None]]


def display_state(job: JsonDict) -> str:
    """The STATE cell: the job state, or the display state of a job in a phase of it."""
    state = _str(job.get("state"), "-")
    phase = job.get("phase")
    if state == "running" and phase == PHASE_PRECOMPILE:
        return PRECOMPILING_STATE
    choosing = bool(job.get("gpu_request")) and not job.get("gpus")
    if (state == "running" and job.get("pending_reason")) or (
        state == "queued" and (phase == PHASE_GPU or (choosing and phase != PHASE_PRECOMPILE))
    ):
        return PENDING_GPU_STATE
    return state


def render_jobs_table(jobs: list[JsonDict], color: bool = False) -> str:
    """`task jobs` table: one row per job, in the given order."""
    table = [
        (
            _str(j.get("jobid"), "-"),
            _str(j.get("task_id"), "-"),
            _str(j.get("submitted_at"), "-"),
            _str(j.get("host"), "-"),
            display_state(j),
        )
        for j in jobs
    ]
    return render_table(JOBS_COLUMNS, table, color)


def text_init(_args: Args, out: Mapping[str, object], color: bool) -> str:
    return message("created", GREEN, "%s in %s" % (out["task_id"], out["path"]), color) + hint(
        "fill every <REQUIRED...> marker, then run `task validate %s`"
        % shlex.quote(str(out["path"])),
        color,
    )


def text_pack(_args: Args, out: Mapping[str, object], _color: bool) -> str:
    """Only the path on stdout, so that `task submit "$(task pack ...)"` works; the errors
    of an invalid package went to stderr."""
    return "%s\n" % out.get("path") if out.get("valid") else ""


def text_validate(_args: Args, out: Mapping[str, object], color: bool) -> str:
    if out.get("valid"):
        return message(
            "valid",
            GREEN,
            "%s, content_hash %s" % (out.get("task_id"), out.get("content_hash")),
            color,
        )
    errors = rows(out, "errors")
    lines = [
        message(
            "invalid", RED, "%d error%s" % (len(errors), "" if len(errors) == 1 else "s"), color
        ).rstrip("\n")
    ]
    for e in errors:
        where = " %s:" % e["path"] if e.get("path") else ""
        lines.append("  %s%s %s" % (paint(_s(e.get("code")), BOLD, color), where, e.get("message")))
    return "\n".join(lines) + "\n"


def text_submit(_args: Args, out: Mapping[str, object], color: bool) -> str:
    row = [_s(out.get(k)) for k in ("jobid", "task_id", "host", "state")]
    text = render_table(("JOBID", "TASK_ID", "HOST", "STATE"), [row], color)
    if out.get("deduplicated"):
        text += message(
            "note",
            YELLOW,
            "already submitted with submission key %s; no new job" % out.get("submission_key"),
            color,
        )
    return text


def text_jobs(_args: Args, out: Mapping[str, object], color: bool) -> str:
    if out.get("error"):
        return ""
    return render_jobs_table(cast(list[JsonDict], rows(out, "jobs")), color)


def _error_field(err: object) -> Fields:
    e = as_dict(err)
    return [("error", "%s: %s" % (e.get("kind"), e.get("message")), RED)] if "kind" in e else []


def _gpus_text(gpus: object) -> str:
    """`0 (GPU-0000...), 2 (GPU-2222...)`, or `-` for none (yet)."""
    rows_ = [as_dict(g) for g in cast(list[object], gpus)] if isinstance(gpus, list) else []
    return ", ".join("%s (%s)" % (_s(g.get("declared")), _s(g.get("uuid"))) for g in rows_) or "-"


def _request_text(req: Mapping[str, object]) -> str:
    """`gpus` as an object, e.g. "1 GPU with >= 40 GiB free, chosen at launch"."""
    n, gb = _s(req.get("count")), req.get("min_free_gb")
    text = "%s idle GPU(s)" % n if gb is None else "%s GPU(s) with >= %s GiB free" % (n, _s(gb))
    listed = req.get("from_uuids")
    if isinstance(listed, list) and listed:
        text += " among %d listed" % len(cast(list[object], listed))
    return text + ", chosen at launch"


HOLDER_TEXT_COMMAND = 100  # characters of a holder's command line in the text output


def _holder_fields(out: Mapping[str, object]) -> Fields:
    """`gpu_holder` lines: who holds the GPUs a waiting job needs (status of the server)."""
    if out.get("gpu_holders_error"):
        return [("gpu_holders", "unknown: %s" % _s(out.get("gpu_holders_error")), YELLOW)]
    holders = rows(out, "gpu_holders")
    if not holders:
        return [("gpu_holders", "none", None)] if isinstance(out.get("gpu_holders"), list) else []
    fields: Fields = []
    for h in holders:
        cmd = _s(h.get("command"), "?")
        if len(cmd) > HOLDER_TEXT_COMMAND:
            cmd = cmd[: HOLDER_TEXT_COMMAND - 3] + "..."
        mib = h.get("used_memory_mib")
        mem = "%.1f GiB" % (mib / 1024) if isinstance(mib, int) else "? GiB"
        job = " (taskmgr job %s)" % h["taskmgr_jobid"] if h.get("taskmgr_jobid") else ""
        text = "GPU %s: pid %s user %s %s%s: %s" % (
            _s(h.get("gpu"), "?"),
            _s(h.get("pid")),
            _s(h.get("user"), "?"),
            mem,
            job,
            cmd,
        )
        fields.append(("gpu_holder", text, YELLOW))
    return fields


def text_status(_args: Args, out: Mapping[str, object], color: bool) -> str:
    meta = as_dict(out.get("_taskmgr"))
    if meta:  # terminal: the job's result.json
        state = _s(meta.get("status"))
        fields: Fields = [
            ("jobid", _s(meta.get("jobid")), None),
            ("task_id", _s(meta.get("task_id")), None),
            ("host", _s(out.get("host")), None),
            ("state", state, STATE_COLORS.get(state)),
            ("return_code", _s(meta.get("return_code")), None),
            ("started_at", _s(meta.get("started_at")), None),
            ("ended_at", _s(meta.get("ended_at")), None),
        ] + _error_field(meta.get("error"))
        for k in ("validation_errors", "warnings"):
            if meta.get(k):
                fields.append((k, _s(meta.get(k)), YELLOW))
        skip = ("host", "complete", "retry_of", "retried_as", "cancel_request", "gpu_request")
        keys = sorted(k for k in out if not k.startswith("_") and k not in skip)
        fields.append(("result", ", ".join(keys) or "-", None))
    else:
        state = display_state(cast(JsonDict, out))
        fields = [
            ("jobid", _s(out.get("jobid")), None),
            ("task_id", _s(out.get("task_id")), None),
            ("host", _s(out.get("host")), None),
            ("state", state, STATE_COLORS.get(state)),
            ("submitted_at", _s(out.get("submitted_at")), None),
            ("gpus", _gpus_text(out.get("gpus")), None),
        ]
        req = as_dict(out.get("gpu_request"))
        if req:
            fields.append(("gpu_request", _request_text(req), None))
        if out.get("pending_reason"):
            fields.append(("pending_reason", _s(out.get("pending_reason")), YELLOW))
        if out.get("cancel_requested"):
            fields.append(("cancel_requested", "yes", YELLOW))
        fields += _holder_fields(out)
        prog = as_dict(out.get("progress"))
        if prog:
            pct = prog.get("percent")
            parts = (_s(prog.get("stage"), ""), "" if pct is None else "%s%%" % pct)
            msg = " ".join(x for x in parts + (_s(prog.get("message"), ""),) if x)
            fields.append(("progress", msg or "-", None))
        fields += _error_field(out.get("error"))
    req = as_dict(out.get("cancel_request"))
    if req:
        how = "" if req.get("via") != "remote" else " (on the server)"
        fields.append(
            ("cancelled_by", "%s at %s%s" % (_s(req.get("by")), _s(req.get("at")), how), YELLOW)
        )
    if out.get("retry_of"):
        fields.append(("retry_of", _s(out.get("retry_of")), None))
    retried = out.get("retried_as")
    if isinstance(retried, list) and retried:
        fields.append(("retried_as", ", ".join(map(str, cast(list[object], retried))), None))
    if out.get("complete") is False:
        fields.append(("note", "some servers were unreachable", YELLOW))
    return render_fields(fields, color)


def text_cancel(_args: Args, out: Mapping[str, object], color: bool) -> str:
    state = _s(out.get("state"))
    if out.get("changed"):
        label, sgr = "cancel requested", GREEN
    elif out.get("cancel_requested"):
        label, sgr = "cancel already requested", YELLOW
    else:
        label, sgr = "nothing to cancel", YELLOW
    job = "job %s on %s is %s" % (
        out.get("jobid"),
        out.get("host"),
        paint(state, STATE_COLORS.get(state), color),
    )
    return message(label, sgr, job, color)


def text_retry(_args: Args, out: Mapping[str, object], color: bool) -> str:
    row = [_s(out.get(k.lower())) for k in RETRY_COLUMNS]
    return render_table(RETRY_COLUMNS, [row], color)


def text_resolve(_args: Args, out: Mapping[str, object], color: bool) -> str:
    state = _s(out.get("state"))
    fields: Fields = [
        ("jobid", _s(out.get("jobid")), None),
        ("host", _s(out.get("host")), None),
        ("state", state, STATE_COLORS.get(state)),
        ("changed", _s(out.get("changed")), None),
    ]
    for k, v in sorted(out.items()):
        if k not in ("jobid", "host", "state", "changed"):
            fields.append((k, _s(v), None))
    return render_fields(fields, color)


def text_fetch(_args: Args, out: Mapping[str, object], color: bool) -> str:
    where = "job %s from %s into %s" % (out.get("jobid"), out.get("host"), out.get("path"))
    text = message("fetched", GREEN, where, color)
    if out.get("partial"):
        text += message("partial", YELLOW, "the job is still %s" % out.get("state"), color)
    for s in rows(out, "skipped"):
        why = "unsafe entry %s (%s)" % (s.get("path"), s.get("reason"))
        text += message("skipped", YELLOW, why, color)
    return text


def text_whoami(_args: Args, out: Mapping[str, object], color: bool) -> str:
    hosts = rows(out, "hosts")
    table = [[_s(h.get("host")), _s(h.get("user")), _s(h.get("hostname"))] for h in hosts]
    text = render_table(("HOST", "USER", "HOSTNAME"), table, color, {}) if table else ""
    if any("user" not in h for h in hosts):
        text += hint(
            "- means the server runs older code; `task host update NAME` updates it", color
        )
    return text


def text_version(_args: Args, out: Mapping[str, object], color: bool) -> str:
    loc = as_dict(out.get("local"))
    commit = _s(loc.get("commit"))
    if loc.get("dirty"):
        commit += " (uncommitted changes)"
    text = render_fields(
        [
            ("local", "taskmgr %s" % _s(loc.get("version")), None),
            ("commit", commit, YELLOW if loc.get("dirty") else None),
            ("code_version", _s(loc.get("code_version"))[:12], None),
            ("protocol", _s(loc.get("protocol_version")), None),
        ],
        color,
    )
    hosts = rows(out, "hosts")
    if hosts:
        table = [
            [
                _s(h.get("host")),
                _s(h.get("code_version"), "none")[:12],
                "up to date" if h.get("up_to_date") else "differs",
            ]
            for h in hosts
        ]
        text += "\n" + render_table(
            ("HOST", "CODE_VERSION", "STATUS"),
            table,
            color,
            {2: {"up to date": GREEN, "differs": YELLOW}},
        )
        if any(not h.get("up_to_date") for h in hosts):
            text += hint("`task host update NAME` installs the local version on a server", color)
    return text


def text_update(_args: Args, out: Mapping[str, object], color: bool) -> str:
    if not out.get("changed"):
        return message("up to date", GREEN, "%s at %s" % (out["path"], out["after"]), color)
    return message(
        "updated", GREEN, "%s from %s to %s" % (out["path"], out["before"], out["after"]), color
    ) + hint("servers keep their code until `task host update NAME` (see `task version`)", color)


def text_host(args: Args, out: Mapping[str, object], color: bool) -> str:
    return hostcmds.render(args, out, color)
