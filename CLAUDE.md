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

## Current phase and next steps

Phase 1 · MVP — in progress. See the milestone on GitHub for open issues.

Next: scaffold + CI → config/schema → SQLite store → policy + hook → executors → git branches →
HTTP transport → ntfy + reports → setup docs → curl E2E.
