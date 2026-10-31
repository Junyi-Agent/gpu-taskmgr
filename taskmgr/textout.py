"""Colored output of the local CLI (`task`); `--detail` prints the JSON object instead.

Every command returns (JSON object, exit code); a renderer turns that object into text.
Everything is colored (stdout and stderr, text and JSON, on a terminal or not) unless
`--plain` is given or NO_COLOR is set.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import cast

# What every local command returns: (JSON object or None, exit code).
CmdResult = tuple[Mapping[str, object] | None, int]

# ANSI SGR codes.
BOLD = "1"
RED = "31"
GREEN = "32"
YELLOW = "33"
MAGENTA = "35"
CYAN = "36"
GRAY = "90"

# STATE shown for a running job whose shim waits to relaunch after a "GPU busy" exit.
GPU_BUSY_STATE = "pending (gpu busy)"

# Color per job state; anything not listed stays uncolored.
STATE_COLORS = {
    "queued": YELLOW,
    "running": CYAN,
    "stopping": MAGENTA,
    "unknown": "1;35",  # bold magenta: needs an operator
    "succeeded": GREEN,
    "failed": RED,
    "invalid_result": RED,
    "timed_out": RED,
    "cancelled": GRAY,
    GPU_BUSY_STATE: YELLOW,  # like queued: waiting for the GPU
}


_color = True  # set once per run by set_color()


def set_color(plain: bool) -> bool:
    """Colors on unless --plain or NO_COLOR; returns the setting."""
    global _color
    _color = not plain and "NO_COLOR" not in os.environ
    return _color


def color_on() -> bool:
    return _color


def paint(text: str, sgr: str | None, color: bool | None = None) -> str:
    """text in the SGR color; `color` defaults to the setting of this run."""
    on = _color if color is None else color
    return "\033[%sm%s\033[0m" % (sgr, text) if on and sgr else text


def say(text: str, sgr: str | None = None) -> None:
    """One line on stderr, in color."""
    _ = sys.stderr.write(paint(text, sgr) + "\n")
    _ = sys.stderr.flush()


def message(label: str, sgr: str | None, text: str, color: bool | None = None) -> str:
    """A one-line result: `label: text`, the label colored (green done, yellow note, red
    failed); every one-line output of the CLI has this form."""
    return "%s %s\n" % (paint(label + ":", sgr, color), text)


def hint(text: str, color: bool | None = None) -> str:
    """`hint: what to run next`."""
    return message("hint", CYAN, text, color)


def note(label: str, sgr: str | None, text: str) -> None:
    """A `label: text` line on stderr (progress, warnings, hints while a command runs)."""
    _ = sys.stderr.write(message(label, sgr, text))
    _ = sys.stderr.flush()


def warn(text: str) -> None:
    note("warning", YELLOW, text)


# JSON highlighting: keys blue, strings green, numbers cyan, true/false/null magenta.
JSON_KEY, JSON_STR, JSON_NUM, JSON_LIT = "34", GREEN, CYAN, MAGENTA


def json_text(obj: object, color: bool | None = None, indent: int | None = 2) -> str:
    """json.dumps(obj, indent=indent, sort_keys=True), highlighted."""
    if not (_color if color is None else color):
        return json.dumps(obj, indent=indent, sort_keys=True)
    sep = "\n" if indent is not None else ""

    def enc(v: object, depth: int) -> str:
        pad = " " * (indent * (depth + 1)) if indent is not None else ""
        end = " " * (indent * depth) if indent is not None else ""
        comma = "," if indent is not None else ", "
        if isinstance(v, Mapping):
            m = cast(Mapping[str, object], v)
            if not m:
                return "{}"
            items = [
                "%s%s: %s" % (pad, paint(json.dumps(k), JSON_KEY, True), enc(m[k], depth + 1))
                for k in sorted(m)
            ]
            return "{" + sep + (comma + sep).join(items) + sep + end + "}"
        if isinstance(v, (list, tuple)):
            seq = cast(Sequence[object], v)
            if not seq:
                return "[]"
            items = [pad + enc(x, depth + 1) for x in seq]
            return "[" + sep + (comma + sep).join(items) + sep + end + "]"
        if isinstance(v, str):
            return paint(json.dumps(v), JSON_STR, True)
        if v is None or isinstance(v, bool):
            return paint(json.dumps(v), JSON_LIT, True)
        return paint(json.dumps(v), JSON_NUM, True)

    return enc(obj, 0)


def state_cell(state: str, color: bool) -> str:
    return paint(state, STATE_COLORS.get(state), color)


def render_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[str]],
    color: bool,
    sgr: Mapping[int, Mapping[str, str]] | None = None,
) -> str:
    """Aligned columns with a bold header. `sgr` maps a column index to {cell text: SGR};
    by default the last column is the job state."""
    if sgr is None:
        sgr = {len(columns) - 1: STATE_COLORS}
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(columns)]

    def line(cells: Sequence[str], codes: list[str | None]) -> str:
        # Pad the plain text first so escape codes never affect the alignment; the last
        # column is not padded, so there is no trailing whitespace.
        padded = [c.ljust(w) for c, w in zip(cells[:-1], widths, strict=False)] + [cells[-1]]
        return (
            "  ".join(paint(c, s, color) for c, s in zip(padded, codes, strict=False)).rstrip()
            + "\n"
        )

    out = line(columns, [BOLD] * len(columns))
    for r in rows:
        out += line(r, [sgr[i].get(c) if i in sgr else None for i, c in enumerate(r)])
    return out


def render_fields(fields: Sequence[tuple[str, str, str | None]], color: bool) -> str:
    """`key: value` lines with aligned values; each field is (key, value, SGR of the value)."""
    width = max((len(k) for k, _, _ in fields), default=0) + 1
    return "".join(
        "%s %s\n" % (paint((k + ":").ljust(width), BOLD, color), paint(v, s, color))
        for k, v, s in fields
    )


def error_text(err: Mapping[str, object], color: bool) -> str:
    """The stderr text of an {"code", "message", "host"?, "details"?} error object."""
    msg = str(err.get("message", ""))
    code = " " + paint("(%s)" % err["code"], GRAY, color) if err.get("code") else ""
    out = "%s %s%s\n" % (paint("error:", RED, color), msg, code)
    details = err.get("details")
    if isinstance(details, Mapping):
        details = cast(Mapping[str, object], details).get("errors")
    if isinstance(details, list):
        for d in cast(list[object], details):
            out += "  " + _detail_line(d) + "\n"
    return out


def _detail_line(d: object) -> str:
    if isinstance(d, Mapping):
        m = cast(Mapping[str, object], d)
        parts = [str(m[k]) for k in ("host", "code", "path") if m.get(k)]
        return "  ".join(parts + [str(m.get("message") or m.get("error") or "")])
    return str(d)
