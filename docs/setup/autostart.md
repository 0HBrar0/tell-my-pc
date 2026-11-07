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

## macOS / Linux

`tell-my-pc autostart` is Windows-only so far, but a per-user service does the same job. Find
the full path first with `command -v tell-my-pc` (often `~/.local/bin/tell-my-pc`) and use it
below.

### Linux (systemd user unit)

Save as `~/.config/systemd/user/tell-my-pc.service`:

```ini
[Unit]
Description=tell-my-pc daemon
After=network-online.target

[Service]
ExecStart=%h/.local/bin/tell-my-pc serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now tell-my-pc
journalctl --user -u tell-my-pc -f      # logs
```

Secrets given as env vars go in an `Environment=NTFY_TOKEN=...` line or an
`EnvironmentFile=` (mode 600). The unit runs only while you are logged in, which is what you
want: it uses your own Claude login.

### macOS (launchd LaunchAgent)

Save as `~/Library/LaunchAgents/dev.tell-my-pc.plist`, with your user name in the paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.tell-my-pc</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/tell-my-pc</string>
    <string>serve</string>
    <string>--log-file</string>
    <string>/Users/YOU/Library/Logs/tell-my-pc.log</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.tell-my-pc.plist
launchctl list | grep tell-my-pc
```

launchd does not read your shell profile, so `claude` must be findable: if `tell-my-pc check`
works in Terminal but the agent can't find `claude`, set `executor.claude_path` in the config.
