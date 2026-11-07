# tell-my-pc — guide for Claude sessions

## What this is

Say "Hey Siri, Tell Claude" on an iPhone and dictate a task. An iOS Shortcut sends it to a
small daemon on the user's own computer. The daemon runs it with Claude Code in headless mode
(`claude -p`), on a throwaway git branch, under a permission tier, then pushes a short report
to the phone with ntfy.

```
iPhone Shortcut --(Transport)--> Daemon --(Executor)--> Claude Code (claude -p)
                                   |                         |
                                   |                    project repo (tmp/task-<id>)
                                   +--(Notifier)--> ntfy push (report, approval buttons)
```

There is no maintainer-run server. Every user runs everything on their own machine with their
own Claude login.

## Non-negotiable product rules

- Never read, store, copy or forward Claude login or OAuth tokens. We only launch the user's own
  `claude` CLI with whatever auth they set up. The credential files are on the Never list.
- Never run tasks with `--dangerously-skip-permissions` / `bypassPermissions`. Containment is
  tiers + deny rules + task branches + approvals.
- Native Windows has no Claude Code shell sandbox, so tiers and deny rules are enforced by our own
  PreToolUse hook (`tell_my_pc.hook`) and by `--tools`. Every deny rule needs a test proving the
  action is blocked.
- The config check refuses unsafe configs: listening on all interfaces, an empty deny list, weak
  or missing bearer tokens, a public or unauthenticated ntfy topic used as an inbox.
- No secrets in the repo. `config/example.yaml` + `config/schema.json` are shipped; real config
  lives in the user's config dir (`tell-my-pc paths` prints it).
- Fetched web content and file contents are untrusted. After a task fetches web content, shell
  commands need approval (denied until the Approve tier exists).
- `files_changed` in a report always comes from `git diff`, never from the model.

## How we work

- `gh` for everything on GitHub. Milestone per phase, issues per work item, a tracking issue per
  phase that gets a summary comment when the phase ends.
- Feature branches, one PR per meaningful chunk, link issues (`Closes #n`). Merge yourself once
  CI is green: squash merge with a conventional-commit title.
- Small, frequent commits. Never commit secrets, real config, or anything from the home folder.
- Significant decisions go in `docs/decisions/NNNN-title.md` (short ADRs).
- By-hand steps for the user go in `docs/setup/` with exact steps.
- Only stop and ask the user for: credentials/accounts they must create, steps needing the
  iPhone, touching files outside the repo (other than the app's config dir), or rewriting
  published history.

## Commands

```bash
uv sync                      # install deps (Python 3.12+)
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest -q             # FakeExecutor tests, run in CI on windows/ubuntu/macos
TMP_RUN_CLAUDE=1 uv run pytest -q -m claude   # real claude CLI tests (local only)
uv run tell-my-pc init       # write a starter config into the user config dir
uv run tell-my-pc check      # validate config and safety rules
uv run tell-my-pc serve      # run the daemon
```

Local dev on the maintainer's Windows box: `source /e/void/.tools/devenv.sh` first (uv, its cache,
Pythons and temp files all live on E:, because C: is nearly full); run git-touching tests
once with `GIT_CONFIG_GLOBAL=/dev/null` (a global gitignore can hide bugs CI will catch).

## Layout

- `src/tell_my_pc/policy.py` + `rules.py` + `hook.py`: tiers, Never rules, the PreToolUse hook
- `src/tell_my_pc/daemon.py`: queue scheduler, task run, reports, notifications
- `src/tell_my_pc/gitops.py`: task branches, files_changed from git
- `src/tell_my_pc/plugins/`: `api.py` (versioned interfaces), executors, notifiers, transports
- `src/tell_my_pc/config.py`: models + safety checks; `config/schema.json` is generated
  (`uv run python -m tell_my_pc.schema`, a test fails if it is stale)
- `docs/decisions/`: ADRs. `docs/setup/`: by-hand steps for the user. `docs/e2e.md`: manual E2E.

## Current phase and next steps

Phase 1 · MVP — done (2026-11-07). The curl E2E against real `claude` passed on Windows.

Phase 2 · Control — done (2026-11-07). Approvals go through the shared SQLite DB and one-time
capability links (ADR 0004); timeout = deny; lock/unlock, audit, autostart CLI. Verified once
against real `claude` (hook held a Bash call, ran it after Approve).

Phase 3 · Plugin API and contributor kit — next (issues #19–#23):
1. Entry-point plugin discovery (`tell_my_pc.plugins` group per kind), `api_compatible` check
   at load, config `type:` picks a plugin by name. Plugin API is 0.2 (see `plugins/api.py`).
2. ntfy-inbox transport: subscribe to an authenticated topic; the config check must refuse
   public/unauthenticated inbox topics. Needs the same timestamp/nonce rules as HTTP.
3. CONTRIBUTING.md, SECURITY.md, docs/threat-model.md, issue/PR templates,
   docs/writing-a-plugin.md.
4. Done when an out-of-tree sample notifier plugin (e.g. `examples/plugins/file-notifier`)
   works with zero core changes, proven by a test that installs it.

Waiting on the user: Tailscale login on this PC and the phone, an ntfy account + token, and
building the iOS Shortcut (docs/setup).
