# iPhone Shortcut: "Tell Claude"

This builds a Shortcut you trigger with **"Hey Siri, Tell Claude"**. It asks what to do, sends
the text to the daemon on your computer over Tailscale, and reads back the reply.

You need, from your computer:

- the **daemon URL**: `http://<your-pc>.<tailnet>.ts.net:8787` (see [tailscale.md](tailscale.md);
  `tailscale status` shows the machine name, the port is `server.port` in your config)
- the **bearer token**: printed once by `tell-my-pc init`, or `server.token` in your config

Your iPhone must have the Tailscale app installed and connected (leave it on, or use
VPN On Demand; see [tailscale.md](tailscale.md)).

## Build it (Shortcuts app, iOS 17 or later)

1. Open **Shortcuts** → tap **+** (top right). Tap the name at the top, rename it to
   **Tell Claude**. This name is the Siri phrase.
2. **Dictate Text**. Search actions for "Dictate Text" and add it.
   - Tap the arrow on the action: set **Stop Listening** to **After Pause**.
   - Language: yours.
3. **Current Date**. Add "Current Date".
4. **Get Time Between Dates**. Add "Get Time Between Dates".
   - First date: tap and type `1 January 1970 00:00 UTC` (tap the field → Choose → type the
     text; Shortcuts turns it into a date).
   - Second date: **Current Date**.
   - In: **Seconds**.
   - Tap the result variable later as **Time Between Dates**.
5. **Random Number**. Add "Random Number", Minimum `100000000`, Maximum `999999999999`.
6. **Get Contents of URL**. Add it, then:
   - URL: `http://<your-pc>.<tailnet>.ts.net:8787/task`
   - Tap the arrow to show options. **Method**: `POST`.
   - **Headers**: add three:
     - `Authorization` = `Bearer <your token>` (one space after "Bearer")
     - `X-TMP-Timestamp` = the **Time Between Dates** variable (from step 4)
     - `X-TMP-Nonce` = the **Random Number** variable (from step 5)
   - **Request Body**: `JSON`. Add one field: key `text`, type Text, value **Dictated Text**
     (the variable from step 2).
   - Optional: add a field `project` (Text) if you always want the same project.
7. **Get Dictionary Value**. Add it: Get **Value** for key `speak` in **Contents of URL**.
8. **Speak Text**. Add it, text = **Dictionary Value**.
9. Tap **Done**.

Say **"Hey Siri, Tell Claude"**, wait for the listening sound, dictate the task, pause. Siri
says something like "Got it. Task 3 f c c for website. Starting now." The full report arrives
later as an ntfy notification.

## Optional extra Shortcuts

Duplicate the Shortcut (long-press → Duplicate) and change only step 6:

| Shortcut name | URL path | Body |
|---|---|---|
| Claude Status | `/status` | empty JSON (no fields) |
| Stop Claude | `/stop` | empty JSON, or `project` = a project name |
| Claude Continue | `/continue` | `text` = Dictated Text (may be empty), optional `project` |

For Claude Status and Stop Claude, remove the Dictate Text action.

## Test it without the iPhone

On the computer, this does exactly what the Shortcut does:

```bash
curl -s -X POST "http://<your-pc>.<tailnet>.ts.net:8787/task" \
  -H "Authorization: Bearer <token>" \
  -H "X-TMP-Timestamp: $(date +%s)" \
  -H "X-TMP-Nonce: $RANDOM$RANDOM$RANDOM" \
  -H "Content-Type: application/json" \
  -d '{"text": "Review my project and tell me what still needs doing."}'
```

Or `tell-my-pc send "Review my project and tell me what still needs doing."`

## Troubleshooting

- **"Not authorized."**: token typo, or the phone's clock is off by more than 5 minutes, or the
  nonce field is empty. Check the three headers.
- **"A server with the specified hostname could not be found"**: the iPhone can't resolve
  the `.ts.net` name, even though Tailscale may be connected. Use the computer's Tailscale
  IP instead (`tailscale ip -4` on the computer, starts with `100.`): URL
  `http://100.x.y.z:8787/task`, and set `server.public_url` to the same base so approval
  buttons work. Tailscale IPs stay the same. Keep Tailscale switched on, since on-demand
  detection only reacts to `.ts.net` names.
- **The request times out**: Tailscale is off on the phone, or the daemon is not running
  (`tell-my-pc status` on the computer).
- **"Which project?"**: say the project name in the task ("...on the website project") or add a
  `project` field.
