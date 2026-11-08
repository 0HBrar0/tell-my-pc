# Changelog

All notable changes. The package follows [Semantic Versioning](https://semver.org); the plugin
API has its own version (see [ADR 0007](docs/decisions/0007-plugin-api-1-0-stability.md)).

## 1.3.0 · 2026-11-08

### Added

- **Dashboard** ([guide](docs/setup/dashboard.md)): `tell-my-pc dashboard` opens a private page
  served by the daemon on `127.0.0.1:8790`. Overview of the last 24 hours (traffic from and to
  the phone, tool calls by verdict, the running task live, approvals waiting), the full
  activity log with filters and search, task reports with every tool call, approvals you can
  answer from the computer, files sent, the kill switch, and the config with secrets hidden.
  Dark and light follow the system. Geist fonts are bundled (SIL OFL), so it works offline.
- The audit log now records what comes in and goes out: each authenticated request with the
  reply Siri spoke, each notification sent (button labels only, links redacted), and the
  dictated text of each task.

### Security

- The dashboard needs a local key (sent as a header, passed to the browser in the URL
  fragment), refuses foreign `Host` headers, accepts only JSON posts, and sends a strict
  content security policy.

## 1.2.0 · 2026-11-07

### Added

- **Tasks on the computer itself** ([ADR 0008](docs/decisions/0008-tasks-on-the-computer-itself.md),
  [guide](docs/setup/computer.md)), opt-in with `computer.enabled`. Say "on my PC", or name no
  project. Claude works in its own workspace folder, with a per-task `tmp/` that is emptied
  afterwards.
  - Allowlisted folders: writes in the workspace and `write_roots`, reads also in
    `read_roots`; anything else asks; `blocked` folders are refused even with an approval.
  - Curated actions without a tap: system info, open Start-menu apps, links, documents,
    media and volume keys, lock screen, screenshot, running programs, Recycle Bin.
    Closing programs and sleep/restart/shut down always ask.
  - Changed files come from a before/after folder snapshot, not from the model.
- `tell-my-pc setup` offers computer tasks (workspace + Downloads).
- Plugin API 1.1: optional `ExecRequest.instructions`.

### Fixed

- Shell commands are also checked for Never and blocked paths in their raw text, so Windows
  backslash paths (mangled by POSIX parsing) are denied instead of asking.

## 1.1.0 · 2026-11-07

### Added

- `tell-my-pc autostart install|status|remove` on **macOS** (a launchd LaunchAgent) and
  **Linux** (a `systemd --user` unit), like the Windows Task Scheduler task. `--dry-run` shows
  the exact file and commands. The service gets explicit config/data paths and your `PATH`,
  never secrets.
- `tell-my-pc setup` offers autostart on every OS.
- CI lints the generated unit with `systemd-analyze verify` and the plist with `plutil -lint`.

### Fixed

- `serve` no longer exits when Tailscale has no address yet (at logon, after sleep, while
  Tailscale reconnects). It waits and logs once a minute; `--no-wait` keeps the old behaviour.
  Before, the Windows task could use up its 3 restarts and stay down until the next logon.
- The Windows task now restarts up to 999 times instead of 3. Run
  `tell-my-pc autostart install` once to update an existing task.

### Install

```bash
uv tool install --force git+https://github.com/0HBrar0/tell-my-pc@v1.1.0
```

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
- macOS and Linux pass the full test suite in CI, but `tell-my-pc autostart` is Windows-only
  (added for macOS and Linux in 1.1.0).
- Day-to-day use so far is on Windows 11 with an iPhone.
- The Windows firewall rule needs an administrator PowerShell; `setup` prints the command.
