# 0004 · Phone approvals through shared state and capability links

Status: accepted · 2026-11-07

## Context
The Approve tier needs a tap on the phone in the middle of a tool call. The PreToolUse hook is
a separate short-lived process; the daemon owns the notifier. ntfy action buttons can call a
URL, but anything in that URL is stored on the ntfy server.

## Decision
- The hook and daemon share the SQLite state file. For an `ask` verdict the hook inserts a
  `pending` approval and polls it. The daemon's approval loop picks up new rows, mints a
  random one-time token (only its SHA-256 is stored), and sends ntfy Approve/Deny buttons that
  POST to `<server.public_url>/approval/<id>?d=approve|deny&t=<token>`.
- That route is not bearer-authenticated: the bearer token must never sit on the ntfy server.
  The capability token is single use, bound to one approval, and dead after the timeout.
- Deadline passes → the hook marks the row `expired` and denies. A late tap gets HTTP 410.
- Approvals need both a notifier and `server.public_url`; without them `ask` is an
  immediate deny. If sending the notification fails, the approval is denied.
- Never rules are not approvable; they never reach this path.
- `lock` (phone or CLI) denies every pending approval and tool call and refuses new tasks.
  Only `tell-my-pc unlock` on the computer lifts it.
- The hook's own timeout is set to the approval timeout + 120 s so Claude Code does not kill
  it while it waits.

## Consequences
- No extra local server or IPC channel; the hook needs only the DB path from its policy file.
- Proven by `tests/test_approvals.py` (waits, approve, deny, timeout = deny, forged/reused
  links refused) and once against the real `claude` CLI (hook held a Bash call ~17 s, then ran
  it after Approve).
