# 0005 · File delivery through a local MCP server and signed links

Status: accepted · 2026-11-07

## Context
Phase 4: "send me the latest screenshot" must put a file on the phone, without letting a task
send arbitrary files (credentials, other projects) and without any maintainer server.

## Decision
- Each task gets exactly one MCP server, ours: `python -m tell_my_pc.mcp_server --policy
  <policy.json>`, passed with `--mcp-config` while `--strict-mcp-config` keeps every other
  server out. It offers `deliver_file(path, note?)` and `list_recent_files(folder?, pattern?)`.
- A file may be sent only if it is inside the task's project or a `delivery.roots` folder
  (checked literally and after resolving symlinks), matches no Never rule, and is under
  `delivery.max_file_mb`. The PreToolUse hook checks the call; the server checks again.
  `delivery.roots` are also readable by the task, so Glob/Read can find files there.
- Accepted files go into a `deliveries` table in the shared state DB (like approvals). The
  daemon sends each one: as an ntfy attachment if it is at most `delivery.attach_max_mb`, and
  always with a link `<public_url>/files/<id>?exp=<epoch>&sig=<hmac>`.
- Links are HMAC-SHA256 signed with a key in the data dir (a Never path for tasks; never in
  config), expire after `delivery.link_ttl_s`, and are served only over the Tailscale
  transport. At download time the file must still be the same resolved path, and a locked
  daemon serves nothing.

## Consequences
- No extra server or dependency; the MCP server is ~200 lines of stdio JSON-RPC.
- Links are bearer-style until they expire: anyone with the URL and tailnet access can
  download within the TTL. That is why they are short-lived and tailnet-only.
- Verified against the real `claude` CLI: asked to "send me the latest screenshot", it called
  `list_recent_files` then `deliver_file` on the newest PNG.
