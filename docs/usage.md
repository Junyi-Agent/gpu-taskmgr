---
title: CLI usage
tags: [taskmgr, cli, usage]
lang: en
up: "[[taskmgr/README]]"
---

# CLI usage

Other language: [[docs/usage.zh-CN]]

A command is a verb and a resource, as in kubectl: `task <verb> <resource> [args]`. The
resources `job`, `package` and `host` also have a plural name (`jobs`, `packages`, `hosts`),
and either works. `task -h` lists the commands, `task <verb> -h` the resources of
a verb, and `task <verb> <resource> -h` its arguments and options.

| Verb | `job` | `package` | `host` |
| --- | --- | --- | --- |
| `create` | submit a package as a new job | a template to fill in by hand | set up a server (or bring it up to date) and add it to the machine list |
| `get` | list the jobs on every server, or show one job | | list the machine list, or one entry |
| `update` | | | update a server's taskmgr-remote to this CLI's code version |
| `delete` | | | remove a server from the machine list |
| `build` | | from project files and options | |
| `validate` | | check a directory or archive | |
| `wait` | wait for the job to end; the exit code tells how | | |
| `logs` | print stdout, stderr or the shim log | | |
| `fetch` | download the job's results | | |
| `cancel` | cancel the job | | |
| `retry` | run a finished job again as a new job | | |
| `resolve` | resolve a job in the `unknown` state | | |
| `enable` / `disable` | | | use a server again / leave it out of submits and queries |
| `ping` | | | check that servers answer |

Other commands: `task build pyz` (the zipapps, [[docs/install]]), `task get user`,
`task get version` and `task update cli` (this local CLI).

```sh
task -h                                # the commands by group (jobs / packages / hosts / others)
task create package AA-01 [--dest DIR] # ./AA-01/ template; fill every <REQUIRED...> marker
task build package SRC... --task-id AA-01 --entry 'python3 run.py' [options] [--dest DIR|FILE.tar.gz]
                                       # build a package from project files; prints its path
task validate package AA-01/           # exit 0 valid, 1 with all errors, 2 bad path
task create job AA-01/                 # round-robin over enabled servers
task create job AA-01.tar.gz --host gpu2
task create job AA-01/ --host gpu2 --submission-key tune-aa01-v3   # idempotent retry
task get jobs [--host H] [--state STATE] [-n N]   # STATE: queued, running, stopping, unknown or a terminal state; N: default 10
task get job <jobid> [--host H]
task wait job <jobid> [--host H] [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task logs job <jobid> [--host H] [--stream stdout|stderr|shim] [--follow]
task fetch job <jobid> [--host H] --dest ./results/
task cancel job <jobid> [--host H]
task retry job <jobid> [--host H]      # creates a new job; prints its jobid
task resolve job <jobid> [--host H] [--as stopped --confirm <jobid>]
task create host NAME [--ssh USER@HOST] [--port N] [--identity-file F]
                      [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--pyz FILE]
                      [--yes] [--plan] [--prune-old-code]
task update host NAME [--pyz FILE] [--yes] [--plan] [--prune-old-code]
                                       # update a listed server's code
task get hosts [NAME]                  # configured servers
task delete host NAME [--yes]          # drop a server from the machine list
task enable host NAME                  # use a server again
task disable host NAME                 # leave a server out of submits and queries for now
task ping host [NAME]                  # check that servers answer
task build pyz [--dest DIR]            # task.pyz and taskmgr-remote.pyz (default: dist/ in the checkout)
task get user                          # user and hostname each server runs as
task get version                       # local version and the code version on each server
task update cli                        # update this local CLI (git pull --ff-only)
```

Every command takes `-o text|json` after the command (`task get jobs -o json`; default
`text`) and `--plain` anywhere on the command line. `task logs job` prints the log as raw
text either way; with `-o json` an error is a JSON object. `--home DIR` sets the local state
directory (default `$TASKMGR_HOME` or `~/.taskmgr`). A path to write to is given with
`--dest`: `-o` with a path is a usage error that says so.

## Command changes

The command line was a list of single verbs before; the old forms are not aliases. An old
command name is a usage error (exit 2) that names the new form, e.g.
``error: `task submit` is now `task create job PKG` ``. Scripts need these changes:

| Old | New |
| --- | --- |
| `task submit PKG` | `task create job PKG` |
| `task jobs` | `task get jobs` |
| `task status ID` | `task get job ID` |
| `task status ID --wait` | `task wait job ID` |
| `task logs ID`, `task fetch ID`, `task cancel ID`, `task retry ID`, `task resolve ID` | `task logs job ID`, `task fetch job ID`, `task cancel job ID`, `task retry job ID`, `task resolve job ID` |
| `task fetch ID --output DIR` | `task fetch job ID --dest DIR` |
| `task init TASK_ID [-o DIR]` | `task create package TASK_ID [--dest DIR]` |
| `task pack SRC... [-o PATH]` | `task build package SRC... [--dest PATH]` |
| `task validate PATH` | `task validate package PATH` |
| `task host add NAME` | `task create host NAME` |
| `task host update NAME` | `task update host NAME` |
| `task host list` | `task get hosts` |
| `task host remove NAME` | `task delete host NAME` |
| `task host enable\|disable\|ping NAME` | `task enable\|disable\|ping host NAME` |
| `python3 -m taskmgr.pyz -o dist` | `task build pyz` (`--dest DIR` for another directory) |
| `--detail` | `-o json` (`-o text` is the default) |
| `-o PATH` / `--output PATH` | `--dest PATH` |
| `task whoami` | `task get user` |
| `task version` | `task get version` |
| `task update` | `task update cli` |

The JSON objects are the same as with `--detail` before.

## Output

All output is colored when stdout and stderr are both terminals: text and `-o json` JSON on
stdout, errors and progress on stderr, `-h` help and usage errors. A pipe or a redirect to a
file gets no colors, so scripts can read `-o json` as is. `FORCE_COLOR=1` turns colors on
anyway; `--plain` (anywhere on the command line) or the environment variable `NO_COLOR` turns
them off in every case.

Every command prints in the same shapes:

| Shape | Used for | Colors |
| --- | --- | --- |
| table | lists (`get jobs`, `retry job`, `create job`, `get hosts`, `ping host`, `get user`, `get version`) | bold header; STATE and status columns by value |
| `key: value` lines | one object (`get job`, `wait job`, `resolve job`, the local part of `get version`) | bold keys; state by value |
| `label: message` | one-line results and notes | label green (done), yellow (note, warning), red (failed), cyan (`hint`) |
| JSON (`-o json`) | the full object | keys blue, strings green, numbers cyan, `true`/`false`/`null` magenta |

```
created: AA-01 in /work/AA-01
hint: fill every <REQUIRED...> marker, then run `task validate package /work/AA-01`
```

Errors with `-o text` are one `error:` line on stderr, with the error code at the end,
and nothing on stdout:

```
error: path does not exist: AA-01 (not_found)
```

With `-o json` an error is `{"error": {"code", "message", "host"?, "details"?}}` on stdout,
plus the `error:` line on stderr.

`task -h`, `task <verb> -h` and `task <verb> <resource> -h` have one layout: a `usage:`
line, a one-sentence description, bold sections with names in one cyan column (`task -h`:
the command groups and `options`; a verb: `resources` and `options`; a command:
`arguments` and `options`), `-h, --help` last, and a line naming the next help to read.

`<jobid>` is enough to find a job: without `--host`, `get job`, `wait job`, `logs job`,
`fetch job`, `cancel job`, `retry job` and `resolve job` ask every enabled server for the
jobid (disabled servers are not searched; a not-found error names them). A jobid found on
several servers → `ambiguous` (exit 2, pass `--host`); not found while some server is
unreachable → `incomplete` (exit 2). `--host` skips the lookup.

`task get jobs` prints a table (a departure from spec §16, by user decision):

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  gpu1           succeeded
```

- A running job whose entry exited 75 because its GPU was held by another process, and
  which waits to be started again, shows STATE `pending-gpu` (see
  [[docs/task-package]], GPU contract). Its state is still `running`; `-o json` and
  `task get job` show the reason in `pending_reason`.
- A job with `precompile` shows `precompiling` while it compiles without GPUs (state
  `running`), then `pending-gpu` while it waits for them (state `queued`). `-o json` and
  `task get job` show `phase` (see [[docs/task-package]], Precompile).
- A job whose `gpus` is an object (GPUs chosen at launch) shows `pending-gpu` while no
  GPUs that meet its request are free (state `queued`); `pending_reason` says how many
  qualify, and `task get job` shows `gpu_request` and, once chosen, `gpus` (see
  [[docs/task-package]], GPU selection).
- `task get jobs` lists the 10 most recently submitted jobs over all queried servers together;
  `-n N` (a positive integer) changes the count. `--host` and `--state` filter first, so
  `-n` counts the jobs that remain. `-o json` returns the same jobs. Each server sends only
  its N newest jobs (server feature `jobs_limit`). `--state` and `-n` apply only to the
  list: with a jobid they are an error (`bad_args`).
- Rows are sorted by submission time (UTC), newest last. STATE has the same color in every command that
  shows it: queued and `pending-gpu` yellow, running and `precompiling` cyan, stopping magenta, unknown bold
  magenta, succeeded green, failed / invalid_result / timed_out red, cancelled gray.
- `--state` takes a job state only (`precompiling` and `pending-gpu` are displays: use
  `--state running` or `--state queued`); any other value is a usage error (exit 2).
- Unreachable servers are reported on stderr (`warning: H unreachable, ...`; a server on
  another protocol version: `warning: H needs an update, ...`), and so are
  the disabled servers that were skipped; with `-o json` too. The exit code is 0 if at
  least one server answered or the machine list is empty, 2 otherwise.
- A server that is down does not hold the command up: ssh gives up connecting after 10 s
  (`ConnectTimeout=10`; an `-o ConnectTimeout=N` in the server's machine entry wins), and a
  query to every server (`get jobs`, the jobid lookup, `get user`, `get version`) waits at most 30 s
  for each server's answer (`TASKMGR_QUERY_TIMEOUT=<seconds>` changes it). The same limit
  applies to every call to a single server (`get job`, `cancel job`, `retry job`,
  `resolve job`, each poll of `wait job`); uploads (`create job`), `logs job` and
  `fetch job` have no limit.
- `-o json` prints the JSON object `{"complete", "errors", "skipped", "jobs"}` instead.
  Each entry of `errors` has `host`, `error` and `code`: `connection_error` when the server
  could not be reached or did not answer in time, `remote_error` when it was reached but
  the remote command gave no JSON reply (it crashed).

| Exit | Meaning |
| --- | --- |
| 0 | success; `wait job`: job succeeded; `get jobs`: at least one server answered, or no server is configured |
| 1 | `wait job`: job ended failed / invalid_result / cancelled / timed_out; `validate package`, `build package`: invalid |
| 2 | parameter, connection, query or operation error; `get jobs` with every server failing; `wait job`: `--timeout` or no connection |
| 3 | `wait job` found the job in `unknown` |

The JSON objects below are what `-o json` prints.

Submit receipt (`task create job`): `{"host", "jobid", "task_id", "submission_key",
"deduplicated", "content_hash", "state"}`. If the outcome is uncertain (connection failure,
or no valid reply: error code `remote_error`), stderr names the chosen host and the
submission key and prints the retry command
`task create job PATH --host H --submission-key K`; the CLI never resubmits to another
server. A package that uses a feature the server's code lacks is
refused before the upload with `unsupported_feature` (exit 2); the message ends with
``run `task update host H` to update it`` (see [[docs/task-package]], Server features).

`task get job` for an unfinished job prints
`{"host", "jobid", "task_id", "state", "pending_reason", "phase", "cancel_requested", "run": {status,
dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries,
last_gpu_busy_at, identity} or null, "progress":
<progress.json or null>, "submitted_at", "gpus", "error", "retry_of", "retried_as",
"gpu_request", "cancel_request", "gpu_holders", "complete"}`. For a finished job it prints the final
`result.json` with `jobid`, `state`, `host`, `complete`, `retry_of`, `retried_as`, `gpu_request` and
`cancel_request` added at the top level.

- `cancel_request`: `null`, or `{"by", "at", "via"}` once someone asked to cancel the job:
  `by` is the `user@hostname` of the CLI that ran `task cancel job` (`via: "cli"`), or the server's
  user and hostname when the cancel was made on the server or by an older CLI
  (`via: "remote"`); `at` is the UTC time of the first request. The text output shows it as
  `cancelled_by: junyi@laptop at 2026-11-03T09:12:44Z`.
- `gpu_holders`: for a job that waits for GPUs (queued with GPUs, `pending-gpu`), the
  processes that hold a CUDA context on its GPUs right now, from `nvidia-smi
  --query-compute-apps` and `/proc` on the server: `[{"gpu": <index>, "uuid", "pid",
  "user", "command" (cut to 200 characters), "used_memory_mib", "taskmgr_jobid"?}]`
  (`taskmgr_jobid` when the process belongs to a taskmgr job). `[]` when no process holds
  them, `null` for a job that waits for no GPU or a server without the `gpu_holders`
  feature. If nvidia-smi fails, `gpu_holders` is `null` and `gpu_holders_error` says why.
  `user` and `command` are `null` for a pid the server's `/proc` does not show (a process in
  another pid namespace). The text output has one line per holder:

  ```
  state:          pending-gpu
  pending_reason: entry exited 75 (GPU busy); retry #12 in 1 s
  gpu_holder:     GPU 0: pid 81234 user root 40.0 GiB: python /root/xbr/mkc/dl/tools/gpu_exec.py ...
  ```

## Packing: `task build package`

`task create package` makes a template to fill in by hand. `task build package` builds a
package from the files of a project and command-line options, validates it, and prints its
path on stdout, and only that, so it composes with `task create job`:

```sh
pkg=$(task build package kernels/ tools/gpu_exec.py src/moe_kernel.py=kernels.py \
        --task-id MO-01 --entry 'python3 run.py --spec spec/moe.yaml' \
        --precompile 'python3 precompile.py kernels.py' --precompile-timeout 1800 \
        --with-cute-helpers --gpus '{"count": 1, "min_free_gb": 40}' \
        --venv /opt/venvs/tune --env CUTE_DSL_ARCH=sm_90a \
        --output-key best_config --output-key speedup \
        --task-md notes/MO-01.md --dest /tmp/MO-01.tar.gz) && task create job "$pkg"
```

Sources (`SRC`, any number):

| Form | Goes to |
| --- | --- |
| a directory `d/` | its contents, at the package root (`d/x.py` becomes `x.py`) |
| a file `f.py` | the package root, as `f.py` |
| `SRC=DEST` | `DEST` inside the package (a file or a directory; `DEST` must stay inside) |

Symlinks are followed: the package gets the contents, so it does not depend on the project
tree. `--exclude GLOB` (repeatable) leaves out names matching GLOB when a directory is
copied. Two sources that write the same file are an error (`conflict`). Names that packing
leaves out anyway (`__pycache__`, `.git`, `*.pyc`, ...; see [[docs/task-package]]) are not
packed.

Then:

- `task.json`: a `task.json` among the sources is the base; otherwise it is written from
  the options, and `--task-id` and `--entry` are required. The options set or extend it:
  `--task-id`, `--entry CMD` and `--precompile CMD` (split like a shell would, no shell runs
  them; `--precompile-timeout SECONDS`), `--gpus` (`none`, `0,1`, `GPU-...`, or JSON such as
  `'[0, 1]'` or `'{"count": 1, "min_free_gb": 40}'`; default none), `--venv PATH`,
  `--require-path PATH`, `--env NAME=VALUE` (merged into `env`), `--timeout SECONDS`,
  `--output-key KEY` (appended to `result.required_outputs`). Repeatable options can be given
  more than once.
- `TASK.md`: `--task-md FILE`, else the sources' `TASK.md`, else a short stub naming the
  sources and the entry command.
- `progress.json`: the sources', else a `not_started` one (with a fixed `updated_at`, so the
  same inputs give the same package and content hash).
- `taskmgr_task.py` is always copied from the template of the running taskmgr (a different
  copy among the sources is replaced, with a warning). `--with-cute-helpers` also copies
  `precompile.py` and `cutecache.py` (see [[docs/task-package]], Precompile).
- The result is validated like `task validate package`. Invalid: exit 1, the errors on stderr,
  nothing written, nothing on stdout.
- `--dest PATH`: a directory, or a file ending in `.tar.gz` / `.tgz` (packed the
  same way `task create job` packs a directory, byte for byte reproducible). Default `./<task_id>`.
  An existing PATH is an error (`exists`, exit 2). `--force` replaces it only when it holds an
  earlier package (a directory with a `task.json`, a `.tar.gz` / `.tgz` file, or a symlink, of
  which only the link is removed); it refuses (`bad_args`, nothing deleted) the current directory
  or one above it, a SRC, and a directory that contains a SRC.

Exit 0 valid (stdout: the path), 1 invalid, 2 bad arguments or sources. `-o json` prints
`{"valid", "path", "archive", "task_id", "content_hash", "features", "source_files",
"warnings"}` (invalid: `{"valid": false, "errors", "path": null}`). `features` are the server
features the package needs (see [[docs/task-package]], Server features).

## Retry and re-runs

A job runs at most once. `task retry job <jobid>` (terminal jobs only; `unknown` →
`resolve_required`, queued/running/stopping → `not_terminal`, both exit 2) creates a **new
job** from the same package at the back of the queue. It prints a table
`JOBID RETRY_OF HOST STATE`; the JSON is
`{"jobid": <new>, "retry_of": <old>, "state": "queued", "changed": true, "enqueue_seq",
"host"}`. The old job keeps its state, logs and result; its `retried_as` lists the new
jobids, and the new job's `retry_of` points back. A resubmission with the original
submission key still returns the original jobid.

When a run is interrupted (reboot, or the entry never started) the job ends `failed` with
`error.kind = "interrupted"` and an automatic re-run is created the same way, keeping the
original queue position (see [[docs/recovery]]). `task wait job` follows such
automatic re-runs: it prints `job X was interrupted; following automatic re-run Y` on stderr
and keeps waiting on Y; output and exit code are those of the last job. Manual retries are
not followed.

## Logs and fetch

- `task logs job` prints the job's log (`--stream`, default stdout). `--follow` keeps printing
  until the run is final or the job is terminal or unknown; on a queued job it waits until
  the job starts. A job that never ran → error `no_run` (exit 2).
- `task fetch job` downloads the job's run files (`job.json`, `runner.json`, `stdout.log`,
  `stderr.log`, `shim.log`, `workdir/`; not `package/`, `package.tar.gz`, `shim.lock`) to
  `<dest>/<host>/<jobid>/` (`--dest DIR` is required). An existing target → `exists` (exit 2), nothing written. A job
  that never ran → `no_run` (exit 2). For an unfinished job stderr warns and the receipt has
  `partial: true`. Receipt: `{"host", "jobid", "state", "partial", "path", "skipped"}`;
  `skipped` lists `{"path", "reason"}` for every entry not written (special files, unsafe
  symlinks), each also a warning on stderr.

## Servers

`task create host NAME --ssh USER@HOST` sets up a GPU server over ssh and adds it to the
machine list; `task create host NAME` checks an existing server and updates it (code,
`remote_root`, systemd user unit and service, linger, machine entry, removal of the code
tree that versions before the zipapp installed). Each change is
shown and asked with `[y/N]`; `--yes` answers yes, except for deleting the code of older
versions, which needs `--prune-old-code` (or a `y` typed in a terminal) and is otherwise
`kept`. `--plan` prints every change the run would make and changes nothing (exit 0
nothing to change, 1 changes planned, 2 a check failed). A second run on an up-to-date
server changes nothing and asks nothing. Progress and prompts go to stderr; stdout gets a summary
of the steps (`-o json`: `{"host", "steps": [{"name", "status", "detail"}], "complete"}`),
exit 0 if every step is `ok`, `changed` or `kept`, otherwise 2. The steps are described in
[[docs/install]].

`task update host NAME` runs the same steps for a server already in the machine list, with
its entry as it is (no ssh options; change those with `task create host`): it uploads the
`taskmgr-remote.pyz` of this CLI's code version (build it first with `task build pyz`),
checks the job directories, restarts the service on the new code (running jobs keep
running) and checks that it answers. It takes `--pyz FILE`, `--yes`, `--plan` and
`--prune-old-code` like `task create host`, also updates a disabled server, and fails with
`unknown_host` for a name not in the list. Use it after `task update cli` or a code change,
when `task get version` shows `differs`, or when `task create job` fails with `unsupported_feature`.

- `task get hosts [NAME]` lists the machine list (or one entry): name, enabled, destination,
  `remote_root`.
- `task delete host NAME` removes a server from the machine list; the server itself is not
  touched.
- `task disable host NAME` marks a server disabled, e.g. while it is down: round-robin
  submits skip it, and the queries over every server (`get jobs`, the jobid lookup, `get user`,
  `get version`) do not ask it, so they do not wait for its timeout. Output names the skipped
  servers (`skipped: disabled, not queried: NAME` on stderr; `"skipped"` in the JSON).
  `task ping host` without NAME skips it too. `--host NAME` and `task ping host NAME` still
  reach it, and nothing changes on the server. `task enable host NAME` turns it back on.
  Both only change `enabled` in `machines.json`.
- `task ping host [NAME]` calls every enabled server (or the named one, disabled or not) and
  shows whether it answers, the round-trip time and the installed code version; exit 2 if
  one did not answer. Disabled servers are listed as `skipped: disabled, not pinged: NAME`
  (`"skipped"` in the JSON).

## get user, get version, update cli

- `task get user [--host H]` asks each server which user and hostname taskmgr runs as there.
- `task update cli` runs `git pull --ff-only` in the local checkout the CLI runs from. Servers
  keep their code until `task update host NAME` installs the new version; when it pulled
  new commits, the output ends with
  ``hint: servers keep their code until `task update host NAME` (see `task get version`)``.
  Run from `task.pyz` it fails with `not_a_checkout` (exit 2): build a new archive from a
  checkout instead (`task build pyz`, see [[docs/install]]).
- `task get version [--host H]` prints the local version (pyproject version, git commit, code
  version, protocol version; run from `task.pyz`, the version and commit are the ones the
  archive was built from, and `dirty` is null) and the code version installed on each server, marked
  `up to date` or `differs`. The code version is the hash `task create host` and
  `task update host` install, so `differs` means `task update host NAME` would upload new
  code; the output then ends with
  ``hint: `task update host NAME` installs the local version on a server``.
