# Start the daemon when you log in

## Windows (Task Scheduler, not a Windows Service)

The daemon runs as you, only while you are logged in, with your own Claude login. No admin
rights are needed for the task itself.

1. Check the config: `tell-my-pc check`
2. Look at the exact PowerShell first, then install:

   ```powershell
   tell-my-pc autostart install --dry-run
   tell-my-pc autostart install
   ```

   This registers a task named `tell-my-pc` with an **At log on** trigger for your account
   (normal, limited rights) and starts it right away. It runs
   `pythonw -m tell_my_pc.cli --config … --data-dir … serve --log-file …` with no console
   window. The explicit `--config` and `--data-dir` mean it doesn't depend on environment
   variables. Logs go to `daemon.log` in the data folder (`tell-my-pc paths`).
   (`schtasks /SC ONLOGON` would need admin rights, which is why we use
   `Register-ScheduledTask`.)
3. Check it: `tell-my-pc autostart status` (state `Running`), then `tell-my-pc status`.

Secrets given as env vars (for example `NTFY_TOKEN`) must be **user** env vars, set with
`[Environment]::SetEnvironmentVariable("NTFY_TOKEN", "tk_...", "User")`, then log off and on.

Remove it with `tell-my-pc autostart remove`.

### Let the phone in (Windows Firewall)

Windows blocks inbound connections on the Tailscale network by default. Allow the daemon's
port **from Tailscale addresses only**. In an **administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "tell-my-pc (Tailscale only)" -Direction Inbound `
  -Protocol TCP -LocalPort 8787 -RemoteAddress 100.64.0.0/10 -Action Allow -Profile Private,Domain
```

If Windows shows a firewall prompt for `pythonw.exe` instead, you can cancel it; the rule above
is narrower than what the prompt would create.

### If the phone can't reach it after a restart or sleep

The daemon waits for Tailscale: until the computer has its Tailscale address, `daemon.log`
shows "no Tailscale IP was found ... Waiting for it" once a minute. If that keeps going, open
the Tailscale app on the computer and make sure it says **Connected**; `tailscale status`
should list your devices. The daemon starts listening by itself as soon as Tailscale is up.

## macOS and Linux

The same command works. It installs a per-user service that runs as you, only while you are
logged in:

```bash
tell-my-pc autostart install --dry-run   # shows the exact file and commands first
tell-my-pc autostart install
tell-my-pc autostart status
tell-my-pc autostart remove
```

| | Linux | macOS |
|---|---|---|
| File | `~/.config/systemd/user/tell-my-pc.service` | `~/Library/LaunchAgents/dev.tell-my-pc.plist` |
| Started with | `systemctl --user enable --now` | `launchctl bootstrap gui/<uid>` |
| Restarts | on failure, after 10 s | when it exits with an error |
| Logs | `daemon.log` in the data folder; start-up errors in `journalctl --user -u tell-my-pc` | `daemon.log`; start-up errors in `launchd.err.log`, both in the data folder |

The service gets explicit `--config` and `--data-dir` paths, and the `PATH` you had when you
installed it, so it finds `claude` even though service managers don't read your shell profile.
If you later install `claude` somewhere else, run `tell-my-pc autostart install` again.

**Secrets given as env vars** (for example `NTFY_TOKEN`) are never written into these files.
Make them visible to services:

- Linux: put `NTFY_TOKEN=tk_...` in `~/.config/environment.d/tell-my-pc.conf` (`chmod 600`),
  then log out and back in.
- macOS: `launchctl setenv NTFY_TOKEN tk_...` (lasts until reboot), or keep the token in the
  config file's `value:` instead, since that file is private to you.

**Linux without systemd** (some containers and WSL setups): `tell-my-pc autostart` says so.
Add `tell-my-pc serve` to your desktop's "Startup Applications" instead.

**Lingering:** a systemd user service stops when you log out. That's intended, because tasks
use your own Claude login. `loginctl enable-linger` would keep it running, but think about
whether you want that.
