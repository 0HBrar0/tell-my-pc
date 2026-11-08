# The dashboard

A private page on the computer that shows everything tell-my-pc does and lets you step in.

```bash
tell-my-pc dashboard
```

That opens it in your browser. It is served by the running daemon on `127.0.0.1:8790` only,
so nothing new is reachable from Tailscale or your network.

## What it shows

- **Overview.** Today's tasks, how many requests came from your phone, how many notifications
  went back, and every tool call by verdict. The line chart shows the last 24 hours:
  traffic from your phone above the line, to your phone below it, the line thickening while a
  task runs, and amber where something needed you. "Now" is the running task as it happens.
  "Needs you" lists approvals waiting for an answer.
- **Activity.** Every event, newest first: requests and what Siri said back, notifications
  sent, tool calls with the hook's verdict, tasks, approvals, files. Filter by direction or
  search; click a row for the full record.
- **Tasks.** Each task's report, the files it changed, and every tool call it made.
- **Approvals.** Approve or deny from the computer, as well as from your phone.
- **Files** sent to your phone, and **Settings**: the kill switch, where the daemon listens,
  the folders computer tasks may use, and the config with secrets hidden.

The verdict mark on a tool call: filled means the policy allowed it, outlined means it asked
you and you approved, struck through means it was denied (dashed when you denied it or didn't
answer).

## Safety

- Every request needs a private key from `<data dir>/dashboard.key`. `tell-my-pc dashboard`
  passes it in the link's `#fragment`, which browsers never send to a server, and the page
  keeps it only for that tab.
- Websites open in your browser can't call the dashboard: they can't add the key header to a
  request without a CORS preflight, which the dashboard never allows, and requests whose
  `Host` isn't `127.0.0.1` or `localhost` are refused.
- The log keeps button labels but never approval or file links (they are one-time keys), and
  never the bearer token.

Turn it off with `dashboard: {enabled: false}`, or move it with `dashboard: {port: 8791}`.
