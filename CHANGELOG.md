# Changelog

All notable changes. The package follows [Semantic Versioning](https://semver.org); the plugin
API has its own version (see [ADR 0007](docs/decisions/0007-plugin-api-1-0-stability.md)).

## 1.0.0 · 2026-11-07

First stable release. Plugin API 1.0.

### Use it

- **"Hey Siri, Tell Claude"**: an iOS Shortcut sends the dictated task over Tailscale to a
  daemon on your own computer, which runs it with `claude -p` and pushes a report with ntfy.
- **`tell-my-pc setup`**: finds your Tailscale address, git projects and screenshots folder,
  generates the token and a secret ntfy topic, refuses unsafe configs, installs autostart on
  Windows and opens a private page with QR codes for the phone.
- Say just the project name to answer "Which project?"; "status", "stop" and "lock" work by voice.
- **Files to the phone**: "send me the latest screenshot" via a local MCP server, as an ntfy
  attachment or an HMAC-signed link that expires.
- **Usage limits**: tasks are held and resume at reset on the same session and branch; an
  optional OpenAI-compatible fallback model takes over in the Read tier (Work only if you opt in).

### Containment

- Read / Work / Approve / Never tiers enforced by an exec-form PreToolUse hook that fails
  closed, also on native Windows. Never runs `--dangerously-skip-permissions`.
- Every task runs on a `tmp/task-<id>` branch; changed files in the report come from `git diff`.
- Approve-tier calls wait for a one-time capability link on the phone; no answer means deny.
- After web content is fetched, shell commands need approval.
- Claude's own login files, the daemon config and system folders are on the Never list.
- The config check refuses binding to all interfaces, weak tokens, an empty deny list and
  public or unauthenticated ntfy inbox topics.

### Extend it

- Entry-point plugins for notifiers, transports, executors and delivery, plugin API 1.0
  (stable until 2.0), a sample out-of-tree plugin, `tell-my-pc plugins`.
- Contributor kit: CONTRIBUTING, SECURITY, threat model, ADRs 0001–0007, docs site at
  <https://0hbrar0.github.io/tell-my-pc/>.

### Install

```bash
uv tool install git+https://github.com/0HBrar0/tell-my-pc@v1.0.0
tell-my-pc setup
```

`pipx install git+https://github.com/0HBrar0/tell-my-pc@v1.0.0` works too, as does the wheel
attached to the GitHub release.

### Known limits

- The fallback executor has been tested against a mock OpenAI-compatible server only.
- macOS and Linux pass the full test suite in CI, but `tell-my-pc autostart` is Windows-only;
  [docs/setup/autostart.md](docs/setup/autostart.md) has a systemd unit and a LaunchAgent.
- Day-to-day use so far is on Windows 11 with an iPhone.
- The Windows firewall rule needs an administrator PowerShell; `setup` prints the command.
