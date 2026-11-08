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

Status: **1.1.0**, stable plugin API 1.0. Docs: <https://0hbrar0.github.io/tell-my-pc/> ·
[Changelog](CHANGELOG.md)

## How it is contained

| Tier | Allowed without asking |
|---|---|
| Read | Read, Grep, Glob, WebSearch, WebFetch inside the project |
| Work | Read tier + edits inside the project + allowlisted commands (tests, builds, `git status/diff/commit`) |
| Approve | anything else waits for a tap on the phone; no answer means no |
| Never | system folders, credential files (incl. Claude's own login), the daemon's config, disk/registry tools, nested shells |

Enforced by a PreToolUse hook on every tool call ([ADR 0002](docs/decisions/0002-enforce-tiers-with-a-pretooluse-hook.md)).

## Quick start

You need [Claude Code](https://docs.claude.com/en/docs/claude-code) (logged in) and
[Tailscale](https://tailscale.com/download) on your computer and iPhone.

```bash
uv tool install git+https://github.com/0HBrar0/tell-my-pc@v1.1.0   # or: pipx install git+...
tell-my-pc setup
```

`setup` finds your Tailscale address and git projects, generates the secrets, refuses unsafe
configs, starts the daemon at logon (Windows, macOS, Linux), and opens a private page with QR codes for the
iPhone part: the ntfy app and the "Tell Claude" Shortcut. Details: [docs/setup](docs/setup/).

Development: `uv sync`, then `uv run pytest -q` (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## More

- [Threat model](docs/threat-model.md): what is protected, and the known limits
- [Writing a plugin](docs/writing-a-plugin.md): notifiers, transports, executors
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) ·
  [Design decisions](docs/decisions/)

## Licence

MIT
