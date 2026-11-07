# Start the daemon when you log in

## Windows (Task Scheduler, not a Windows Service)

The daemon runs as you, only while you are logged in, with your own Claude login.

1. Secrets the daemon reads from env vars must be **user** env vars, because Task Scheduler
   does not see variables set only in a terminal:

   ```powershell
   [Environment]::SetEnvironmentVariable("NTFY_TOKEN", "tk_...", "User")
   ```

   (Or keep `server.token.value` in your private config file, which `tell-my-pc init` does.)
2. Check the config: `tell-my-pc check`
3. See the exact command first, then install:

   ```powershell
   tell-my-pc autostart install --dry-run
   tell-my-pc autostart install
   ```

   This creates a task named `tell-my-pc` with trigger **At log on**, **Run only when user is
   logged on** and normal (limited) rights. It runs `pythonw -m tell_my_pc.cli serve` with no
   console window and logs to `daemon.log` in the data folder (`tell-my-pc paths`).
4. Log off and on again, then `tell-my-pc status`.

Remove it with `tell-my-pc autostart remove`; inspect it with `tell-my-pc autostart status`
or in Task Scheduler → Task Scheduler Library → tell-my-pc.

## macOS / Linux

Not built in yet. A `launchd` LaunchAgent or a `systemd --user` unit that runs
`tell-my-pc serve` works the same way. Contributions welcome.
