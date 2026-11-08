# tell-my-pc

Say **"Hey Siri, Tell Claude"**, dictate a task, and put the phone away. A small daemon on your
own computer runs it with [Claude Code](https://docs.claude.com/en/docs/claude-code) in headless
mode, on its own git branch, and pushes a short report back to your phone with
[ntfy](https://ntfy.sh).

> "Run the tests and fix the failures." · "Send me the latest screenshot."
> · "Continue what you were doing on the website project."

- **Self-hosted only.** Your machine, your Claude login, your Tailscale network, your ntfy topic.
  Nobody runs a server for you.
- **Contained.** Every tool call passes a permission hook (Read, Work, Approve, Never tiers) and
  runs on a throwaway `tmp/task-<id>` branch. Never `--dangerously-skip-permissions`.
- **You stay in charge.** Risky steps wait for a tap on your phone; "lock" stops everything.
- **Honest reports.** Changed files come from `git diff`, not from the model.

## Install

You need Python 3.12+, [Claude Code](https://docs.claude.com/en/docs/claude-code) (logged in)
and [Tailscale](https://tailscale.com/download) on the computer and the iPhone.

```bash
uv tool install git+https://github.com/0HBrar0/tell-my-pc   # or: pipx install git+https://github.com/0HBrar0/tell-my-pc
tell-my-pc setup
```

`setup` finds your Tailscale address and git projects, generates the secrets, refuses unsafe
configs, starts the daemon at logon on Windows, and opens a private page with QR codes for the
phone part.

## Set up, step by step

1. [Tailscale](setup/tailscale.md): reach the PC from the phone, privately
2. [ntfy](setup/ntfy.md): reports and approval buttons on the phone
3. [The "Tell Claude" Shortcut](setup/iphone-shortcut.md)
4. [Start at logon](setup/autostart.md)
5. Optional: [tasks on the computer itself](setup/computer.md) (open apps, media keys,
   files in Downloads), [files to the phone](setup/files.md),
   [fallback models](setup/fallback.md)

## How it works

```
iPhone Shortcut --(Transport)--> Daemon --(Executor)--> Claude Code (claude -p)
                                   |                         |
                                   |                    project repo (tmp/task-<id>)
                                   +--(Notifier)--> ntfy push (report, approval buttons)
```

| Tier | Allowed without asking |
|---|---|
| Read | read, search and web lookups inside the project |
| Work | Read + edits inside the project + allowlisted commands (tests, builds, `git status/diff/commit`) |
| Approve | anything else waits for a tap on the phone; no answer means no |
| Never | system folders, credential files (incl. Claude's own login), the daemon's config, disk and registry tools, nested shells |

- [Threat model](threat-model.md): what is protected, and the known limits
- [Design decisions](decisions/)
- [Writing a plugin](writing-a-plugin.md): notifiers, transports, executors, delivery

## Project

[Source on GitHub](https://github.com/0HBrar0/tell-my-pc) ·
[Changelog](https://github.com/0HBrar0/tell-my-pc/blob/main/CHANGELOG.md) ·
[Contributing](https://github.com/0HBrar0/tell-my-pc/blob/main/CONTRIBUTING.md) ·
[Security policy](https://github.com/0HBrar0/tell-my-pc/blob/main/SECURITY.md) · MIT licence
