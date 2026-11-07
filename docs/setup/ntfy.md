# ntfy: reports and approval buttons on your phone

tell-my-pc pushes reports to an [ntfy](https://ntfy.sh) topic. Anyone who knows a topic name
on the public ntfy.sh server can read it, so use an **account with an access token and a
reserved topic**, or run your own ntfy server.

## Option A: ntfy.sh account (simplest)

1. Create an account at <https://ntfy.sh/signup>.
2. Reserve a topic: <https://ntfy.sh/account> → **Reserved topics** → **Reserve topic**. Use a
   long random name, e.g. the one `tell-my-pc init` printed (`tmp-…`). Set access to
   **Only I can publish and subscribe**. (Reserving topics needs a paid tier on ntfy.sh. If
   you stay on the free tier, keep the random topic name secret; it is the only protection.)
3. Create an access token: <https://ntfy.sh/account> → **Access tokens** → **Create access
   token**. Copy it (starts with `tk_`).
4. Give it to the daemon as an environment variable (do not paste it into the repo):
   - Windows (PowerShell): `[Environment]::SetEnvironmentVariable("NTFY_TOKEN", "tk_…", "User")`,
     then open a new terminal.
   - macOS/Linux: add `export NTFY_TOKEN=tk_…` to your shell profile.
5. Config:

   ```yaml
   notifier:
     type: ntfy
     server: https://ntfy.sh
     topic: tmp-your-random-topic
     token:
       env: NTFY_TOKEN
   ```

6. `tell-my-pc check` should no longer warn about the missing token.

## Option B: your own ntfy server

Run ntfy on a machine in your tailnet (<https://docs.ntfy.sh/install/>), enable auth
(`auth-default-access: deny-all`), create a user and token with `ntfy token add <user>`, and set
`server:` to its `https://` or `*.ts.net` URL.

## Phone

1. Install **ntfy** from the App Store.
2. If you use an account: app **Settings** → **Users** → add `https://ntfy.sh` with your
   username and password.
3. **+** → subscribe to your topic (same server).
4. Allow notifications when asked.

Reports arrive as notifications titled `<project>: done (<task id>)`.
