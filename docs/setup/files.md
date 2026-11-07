# Getting files on your phone

Ask "Tell Claude, send me the latest screenshot" (or any file in a project). Claude finds the
file and you get an ntfy notification with:

- the file **attached**, if it is small (up to `delivery.attach_max_mb`, default 2 MB), and
- a **link** that downloads it over Tailscale and stops working after `delivery.link_ttl_s`
  (default 1 hour).

## Which files can be sent

Files inside the task's project, plus any folder you list. Never-rule files (`.env`, keys,
credentials, system folders) can't be sent from anywhere.

```yaml
server:
  public_url: http://100.x.y.z:8787   # your PC's Tailscale IP; needed for links
delivery:
  roots:
    - ~/Pictures/Screenshots          # Windows' default screenshot folder
  link_ttl_s: 3600
  attach_max_mb: 2
  max_file_mb: 200
```

Folders in `delivery.roots` also become readable for tasks, so only list folders you are
happy for Claude to look at.

## If it doesn't arrive

- `tell-my-pc audit` shows `file_queued`, `file_sent` and `file_downloaded` entries.
- A link that says "expired or is not valid" is past its time: ask again.
- Without `server.public_url`, only small files can be sent (as attachments).
