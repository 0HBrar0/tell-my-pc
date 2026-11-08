# 0008 · Tasks on the computer itself, not just in projects

Status: accepted · 2026-11-07

## Context

Users want "open Spotify", "what's my battery", "tidy my Downloads", not only work inside a git
project. Outside a project there is no task branch to throw away and no `git diff` to report,
so the project containment (ADR 0002, 0003) does not carry over by itself.

## Decision

An optional `computer:` config section adds one more target, spoken as "computer" or "my PC".
With `default_target: true`, tasks that name no project go there too.

- **Its own workspace.** Claude runs in `computer.workspace` (for example `E:\tell-my-pc`), a
  folder it fully controls. Each task gets `workspace/tmp/<task-id>` for scripts and scratch
  files; the daemon deletes it when the task ends.
- **Allowlisted folders, not a blocklist.** Writes are allowed in the workspace and
  `write_roots`; reads also in `read_roots`. Anything else asks for approval on the phone.
  `blocked` folders become Never rules: denied, and no approval can unlock them. The built-in
  Never list (credentials, Claude's login, system folders, the daemon's config) still applies.
- **Curated actions.** The task's MCP server gets `pc_*` tools that do a fixed thing with
  checked arguments: list/open Start-menu apps, open an http(s) URL, open a document or folder
  in an allowed folder, media and volume keys, lock the screen, take a screenshot, system info,
  list processes, send a file in a writable folder to the Recycle Bin. These run without a tap.
  `pc_close_app` and `pc_power` (sleep, restart, shut down) always ask.
- **Anything else asks.** Shell commands follow the Work allowlist; any other command or a
  path outside the writable folders goes to the phone with the exact command shown. Never
  `--dangerously-skip-permissions`.
- **Honest reports.** `files_changed` comes from a before/after snapshot of the writable
  folders (size and modification time), not from the model, just as `git diff` does for
  projects.
- No fallback model for computer tasks: they wait for Claude's usage limit to reset.

## Consequences

Opening an app is code execution, so `pc_open_app` only launches apps that are already in the
Start menu (by AppID), and `pc_open_path` refuses executables, scripts and shortcuts. Deletes
by the curated tool go to the Recycle Bin, so they can be undone. Shell `rm` or `del` asks
first. Snapshots cover at most 20,000 files per folder; beyond that the report says it could
not track changes.
