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

Status: early development (Phase 1 · MVP). Not ready for daily use yet.

## Quick start (development)

```bash
uv sync
uv run tell-my-pc init     # writes a starter config into your user config dir
uv run tell-my-pc check
uv run tell-my-pc serve
```

Then follow [docs/setup](docs/setup/) for the iPhone Shortcut, Tailscale and ntfy.

## Licence

MIT
