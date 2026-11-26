# Task Manager

[中文](README.zh-CN.md)

A GPU task manager: the local `task` CLI packs, submits and monitors jobs on remote GPU servers over SSH.

Design documents are numbered in `./agent/desc-<n>.md`: `desc-1.md` is the requirements spec, `desc-2.md` adds design decisions, `desc-3.md` covers the toolchain (pre-commit, uv). If you are an AI agent implementing this project, read them in order.

If you are an AI agent using this tool, skip the spec. Read `./taskmgr/README.md`, or run `task --help`.

If you are a human, run `/quiz` from time to time to check your understanding of the tool's design.
