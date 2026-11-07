# tell-my-pc

Say **"Hey Siri, Tell Claude"**, dictate a task, and put the phone away. A small daemon on your
own computer runs the task with [Claude Code](https://docs.claude.com/en/docs/claude-code) in
headless mode, on its own git branch, and pushes a short report back to your phone.

> "Run the tests and fix the failures." · "Review my project and tell me what still needs doing."
> · "Continue what you were doing on the website project."

- **Self-hosted only.** No server run by the maintainers. You use your own machine, your own
  Claude login, your own Tailscale network and your own ntfy topic.
- **Contained.** Tasks run under permission tiers (Read, Work, Approve, Never) enforced by a hook,
  on a throwaway `tmp/task-<id>` branch. It never uses `--dangerously-skip-permissions`.
- **Honest reports.** The list of changed files comes from `git diff`, not from the model.

Status: Phase 1 (MVP) works end to end: curl imitating the Shortcut → Claude runs and fixes
tests on a task branch → ntfy report. Phone approvals (Phase 2) are next. Not ready for daily
use yet.

## How it is contained

| Tier | Allowed without asking |
|---|---|
| Read | Read, Grep, Glob, WebSearch, WebFetch inside the project |
| Work | Read tier + edits inside the project + allowlisted commands (tests, builds, `git status/diff/commit`) |
| Approve | anything else needs a tap on the phone (Phase 2; until then: denied) |
| Never | system folders, credential files (incl. Claude's own login), the daemon's config, disk/registry tools, nested shells |

Enforced by a PreToolUse hook on every tool call ([ADR 0002](docs/decisions/0002-enforce-tiers-with-a-pretooluse-hook.md)).

## Quick start (development)

```bash
uv sync
uv run tell-my-pc init     # writes a starter config into your user config dir
uv run tell-my-pc check
uv run tell-my-pc serve
```

Then follow [docs/setup](docs/setup/) for the iPhone Shortcut, Tailscale and ntfy.

## More

- [Threat model](docs/threat-model.md): what is protected, and the known limits
- [Writing a plugin](docs/writing-a-plugin.md): notifiers, transports, executors
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) ·
  [Design decisions](docs/decisions/)

## Licence

MIT
