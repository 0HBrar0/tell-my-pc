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

## Optional: ntfy inbox (send tasks without Tailscale)

If the phone can't use Tailscale, it can publish tasks to a **second, private** ntfy topic
that the daemon subscribes to. Anyone who can publish there can give your computer tasks, so
tell-my-pc refuses to start the inbox unless all of this holds:

- The topic is reserved/protected: `GET https://ntfy.sh/<topic>/auth` **without** a token
  must fail. On ntfy.sh this needs a reserved topic set to "Only I can publish and
  subscribe"; on your own server, `auth-default-access: deny-all`.
- It has an ntfy access token (`NTFY_INBOX_TOKEN`) and a shared **inbox secret** of 32+
  characters (`TELL_MY_PC_INBOX_SECRET`), different from the HTTP bearer token. The secret
  sits in messages on the ntfy server, which is why it is separate.
- The topic is at least 24 random characters and is **not** your notifications topic.

```yaml
inbox:
  server: https://ntfy.sh
  topic: tmp-inbox-<24+ random characters>
  token: {env: NTFY_INBOX_TOKEN}
  secret: {env: TELL_MY_PC_INBOX_SECRET}
```

In the iOS Shortcut, change step 6 of [iphone-shortcut.md](iphone-shortcut.md): URL
`https://ntfy.sh/<inbox topic>`, header `Authorization: Bearer <ntfy token>`, and a JSON
body with `secret`, `ts` (Time Between Dates), `nonce` (Random Number) and `text`. There is
no spoken reply; the answer arrives as a notification. Optional `cmd`: `status`, `stop`,
`lock`, `continue`.

## Approve / Deny buttons

When a task wants to do something outside its tier (for example `git push`), you get an
**Approve?** notification with two buttons. Tapping one makes your phone call the daemon over
Tailscale, so set where the phone reaches it:

```yaml
server:
  public_url: http://<your-pc>.<tailnet>.ts.net:8787
policy:
  approval_timeout_s: 300   # no answer in time counts as Deny
```

Without `public_url`, anything outside the tier is denied straight away. The buttons carry a
one-time link, never your bearer token. To stop everything at once, run the **Lock** Shortcut
(`POST /lock`) or `tell-my-pc lock`; only `tell-my-pc unlock` on the computer undoes it.
