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

Phase 3 · Plugin API and contributor kit — done (2026-11-07). Entry-point registry
(`plugins/registry.py`), `also_notify` fan-out, `tell-my-pc plugins`, sample plugin in
`examples/plugins/file-notifier` (CI installs it with `uv run --with`), ntfy inbox transport
(refuses open topics at start-up), CONTRIBUTING, SECURITY, `docs/threat-model.md`,
`docs/writing-a-plugin.md`, issue/PR templates. Plugin API 0.2 (1.0 since v1.0.0).

Phase 4 · Files — done (2026-11-07). Local MCP server `tell_my_pc.mcp_server`
(`deliver_file`, `list_recent_files`) passed per task with `--mcp-config`; `delivery.roots`
config; `deliveries` table; ntfy attachments for small files; HMAC-signed expiring links at
`/files/<id>` (ADR 0005). Verified with real `claude`: "send me the latest screenshot" worked.

Phase 5 · Fallback — done (2026-11-07). Usage-limit detection (epoch and "resets 3pm"
forms), `held` tasks that re-queue at reset and resume their session/branch, OpenAI-compatible
`FallbackExecutor` (policy-checked tool loop, approvals forced off), Read tier by default,
Work only with `fallback_work` on `fallback/task-<id>` (ADR 0006). Not yet run against a real
local model (no Ollama on the maintainer PC).

Phase 6 · v1.0 — done (2026-11-07), released as v1.0.0. `tell-my-pc setup` wizard
(`setup_wizard.py`: Tailscale IP, git projects, screenshots folder, token, secret ntfy topic,
safety refusal, autostart, private QR page from `phone_page.py`), version 1.0.0 with a CI
wheel-install check, plugin API 1.0 frozen (ADR 0007), docs site from `docs/` on GitHub
Pages (https://0hbrar0.github.io/tell-my-pc/, `tests/test_docs.py` keeps links inside
docs/), `CHANGELOG.md`.

Releasing: bump `version` in pyproject.toml and `__version__`, add a CHANGELOG section,
merge, then `git tag -a vX.Y.Z` on main, push the tag, `uv build` and
`gh release create vX.Y.Z dist/*` with the CHANGELOG section as notes. Plugin API changes
follow ADR 0007 (additive in 1.x).

1.1.0: `autostart.py` plans and runs autostart for Windows (Task Scheduler), Linux
(`systemd --user`) and macOS (launchd); plans are pure data so tests cover every OS, and CI
lints the real files (`scripts/ci_autostart_files.py`).

1.2.0: computer tasks (ADR 0008). `computer.py` (target, workspace + tmp, snapshot diff),
`pc_actions.py` (curated actions, Windows-first), `pc_*` tools in `mcp_server.py`, decisions
in `policy._decide_pc`, `PolicySpec.write_roots`/`pc_actions`. Tests in
`tests/test_computer.py`; the FakeExecutor only simulates `pc_*` calls. Verified with real
`claude` on the maintainer PC (system info, Downloads write, blocked folder, app open,
Recycle Bin, screenshot).

Next ideas (no milestone yet): real local-model run of the fallback executor, PyPI publishing
(needs a PyPI account and a trusted publisher set up by the user).

Running install on the maintainer PC: `bash /e/void/.tools/update-tell-my-pc.sh` reinstalls
from `main` and restarts the logon task. Real config: `E:/void/.tell-my-pc/config`.
The phone side (Tailscale, Shortcut, ntfy app on a random topic without a token) is set up and
working; an ntfy account + token would turn on file attachments.
