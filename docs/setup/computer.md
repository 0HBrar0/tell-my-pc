# Tasks on the computer itself

Besides your projects, tell-my-pc can do things on the computer: "open Spotify", "pause the
music", "what's my battery", "move the PDFs in Downloads into a folder", "lock my PC",
"send me a screenshot". The design is in
[ADR 0008](../decisions/0008-tasks-on-the-computer-itself.md).

Say "on my PC" or "computer", or name no project at all (with `default_target: true`, tasks
that name no project go to the computer).

## Turn it on

`tell-my-pc setup` asks. Or add this to your config and run `tell-my-pc check`:

```yaml
computer:
  enabled: true
  workspace: E:/tell-my-pc        # its own folder; scripts go in tmp/, emptied after each task
  write_roots: [E:/Downloads]     # may also change files here
  read_roots: [E:/Study]          # may read, not change
  blocked: [E:/Softwares, E:/Games, E:/Pictures]   # never, not even with an approval
  default_target: true            # tasks naming no project go here
  actions: true                   # the curated actions below
```

Everything that isn't listed asks for approval on your phone first. Blocked folders are
refused outright. The built-in Never list (credentials, Claude's login, system folders,
tell-my-pc's own config) applies as always. `check` refuses a workspace or write root that is
a whole drive, your whole home folder, a system folder, or tell-my-pc's own folders.

## What runs without asking

| Action | Example |
|---|---|
| System info | "what's my battery", "how much space is left" |
| Open an app from the Start menu | "open Spotify" |
| Open a link in the browser | "open youtube.com" |
| Open a document or folder in an allowed folder | "open my notes PDF" (never programs or scripts) |
| Media and volume keys | "pause", "next song", "volume to 30" |
| Lock the screen | "lock my PC" |
| Screenshot | "send me a screenshot" |
| List running programs | "what's using my memory" |
| Files in the workspace and write roots | create, edit, move, rename; delete goes to the Recycle Bin |

## What always asks first

- Closing a program ("close Chrome").
- Sleep, restart, shut down. Restart and shut down wait 60 seconds; "cancel the shutdown"
  stops them.
- Any other shell or PowerShell command. The phone shows the exact command.
- Anything in a folder that isn't listed.

## The report

There is no git branch, so the changed files in the report come from a before/after snapshot
of the workspace and write roots (size and modification time), not from what Claude says.
Folders with more than 100,000 files are not tracked; the report says so.

Opening apps, media keys, locking, screenshots and the Recycle Bin are Windows-only for now.
On macOS and Linux, system info, links, opening documents and file work in the allowed
folders all work.
