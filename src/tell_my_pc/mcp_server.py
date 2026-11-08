"""A tiny local MCP server (stdio) that lets a task send files to the phone.

Started per task by the Claude Code executor::

    python -m tell_my_pc.mcp_server --policy <task policy.json>

Tools:

* ``deliver_file(path, note?)``: queue a file for the phone. Only files in the task's
  project or ``delivery.roots``, never Never-rule paths, and under the size limit.
* ``list_recent_files(folder?, pattern?, limit?)``: newest files in the allowed folders,
  so "send me the latest screenshot" works without guessing names.

The PreToolUse hook also checks ``deliver_file`` calls; this server re-checks every path
itself (defence in depth) and records accepted files in the state DB for the daemon to send.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .delivery import DeliveryRefused, check_file
from .policy import PolicySpec, _PathCheck

SERVER_NAME = "tell_my_pc"
DEFAULT_PROTOCOL = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "deliver_file",
        "description": (
            "Send a file from this computer to the user's phone (a notification with the file "
            "attached, or a link that expires). Use it when the user asks you to send, share "
            "or show them a file. Only files in the project or the allowed delivery folders "
            "can be sent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or project-relative path."},
                "note": {"type": "string", "description": "One short line shown with the file."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_recent_files",
        "description": (
            "List the newest files in the project or the allowed delivery folders (for example "
            "the screenshots folder), newest first, with their modification times."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "One of the allowed folders; default: all of them.",
                },
                "pattern": {"type": "string", "description": "File name glob, e.g. *.png"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
]


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


_PATH = {"path": {"type": "string", "description": "Absolute path."}}
PC_TOOLS: list[dict[str, Any]] = [
    _tool("pc_system_info", "Time, battery, memory, uptime and free disk space.", {}, []),
    _tool(
        "pc_list_apps",
        "Apps in the Start menu (names to use with pc_open_app).",
        {"query": {"type": "string", "description": "Only names containing this."}},
        [],
    ),
    _tool(
        "pc_open_app",
        "Open an app from the Start menu by name, e.g. Spotify, Notepad, Calculator.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "pc_open_url",
        "Open an http(s) link in the default browser on the computer.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _tool(
        "pc_open_path",
        "Open a document or folder on the computer with its default app (not programs or scripts).",
        _PATH,
        ["path"],
    ),
    _tool(
        "pc_media",
        "Media and volume keys. set_volume takes a percentage in `times`.",
        {
            "action": {
                "type": "string",
                "enum": [
                    "play_pause",
                    "next",
                    "previous",
                    "stop",
                    "volume_up",
                    "volume_down",
                    "mute",
                    "set_volume",
                ],
            },
            "times": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ["action"],
    ),
    _tool("pc_lock_screen", "Lock the computer's screen.", {}, []),
    _tool(
        "pc_screenshot",
        "Take a screenshot of the whole screen; returns the saved path (send it with "
        "deliver_file).",
        {},
        [],
    ),
    _tool("pc_list_processes", "Running programs, by memory use.", {}, []),
    _tool(
        "pc_recycle",
        "Move a file or folder to the Recycle Bin (only in the folders this task may change).",
        _PATH,
        ["path"],
    ),
    _tool(
        "pc_close_app",
        "Ask a running program to close, by process name like spotify.exe. Needs the user's "
        "approval.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    _tool(
        "pc_power",
        "Sleep, restart or shut down the computer, or cancel a pending restart/shutdown. "
        "Needs the user's approval.",
        {"action": {"type": "string", "enum": ["sleep", "restart", "shutdown", "cancel"]}},
        ["action"],
    ),
]

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}


class Server:
    def __init__(self, spec: PolicySpec) -> None:
        self.spec = spec

    # ------------------------------------------------------------------ tools

    def deliver_file(self, args: dict[str, Any]) -> str:
        checked = check_file(self.spec, str(args.get("path") or ""))
        if not self.spec.db_path:
            raise DeliveryRefused("file delivery is not available (no state database)")
        from .store import Store

        store = Store(self.spec.db_path)
        try:
            if store.locked():
                raise DeliveryRefused("tell-my-pc is locked")
            store.add_delivery(
                self.spec.task_id,
                str(checked.path),
                checked.size,
                str(args.get("note") or ""),
                self.spec.delivery_ttl_s,
            )
            store.audit(
                "file_queued",
                {"path": str(checked.path), "size": checked.size},
                task_id=self.spec.task_id or None,
            )
        finally:
            store.close()
        mins = max(1, int(self.spec.delivery_ttl_s // 60))
        return (
            f"Queued {checked.path.name} ({_human(checked.size)}) for the user's phone. "
            f"It arrives as a notification; any link in it expires in {mins} min."
        )

    def list_recent_files(self, args: dict[str, Any]) -> str:
        checker = _PathCheck(self.spec)
        roots = [self.spec.project_dir, *self.spec.delivery_roots]
        if args.get("folder"):
            folder = str(args["folder"])
            if not any(
                checker.forms(folder)[1] == checker.forms(r)[1]
                or checker.forms(folder)[1].startswith(checker.forms(r)[1] + "/")
                for r in roots
            ):
                raise DeliveryRefused(f"{folder} is not an allowed folder")
            roots = [folder]
        pattern = str(args.get("pattern") or "*")
        limit = int(args.get("limit") or 10)
        found: list[tuple[float, str]] = []
        seen = 0
        for root in roots:
            base = os.path.realpath(os.path.expanduser(root))
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for f in filenames:
                    seen += 1
                    if seen > 50_000:
                        break
                    if not fnmatch.fnmatch(f.lower(), pattern.lower()):
                        continue
                    full = os.path.join(dirpath, f)
                    if checker.never(full):
                        continue
                    try:
                        found.append((os.path.getmtime(full), full))
                    except OSError:
                        continue
        found.sort(reverse=True)
        if not found:
            return "No matching files in the allowed folders."
        import time

        lines = [
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(m))}  {p}" for m, p in found[:limit]
        ]
        return "Newest first:\n" + "\n".join(lines)

    def pc(self, name: str, args: dict[str, Any]) -> str:
        """Run a curated computer action. The hook already decided; paths are re-checked."""
        from . import pc_actions as pc

        if not self.spec.pc_actions:
            raise DeliveryRefused("computer actions are only available in computer tasks")
        checker = _PathCheck(self.spec)

        def checked_path(write: bool) -> Path:
            raw = str(args.get("path") or "")
            if not raw:
                raise DeliveryRefused("no path given")
            hit = checker.never(raw)
            if hit:
                raise DeliveryRefused(f"{raw} matches the Never rule {hit}")
            ok = checker.writable(raw) if write else checker.readable(raw)
            if not ok:
                raise DeliveryRefused(f"{raw} is outside the allowed folders")
            return Path(checker.forms(raw)[1])

        simple = {
            "pc_system_info": pc.system_info,
            "pc_lock_screen": pc.lock_screen,
            "pc_list_processes": pc.list_processes,
        }
        if name in simple:
            return simple[name]()
        if name == "pc_list_apps":
            return pc.list_apps(str(args.get("query") or ""))
        if name == "pc_open_app":
            return pc.open_app(str(args.get("name") or ""))
        if name == "pc_open_url":
            return pc.open_url(str(args.get("url") or ""))
        if name == "pc_open_path":
            return pc.open_path(checked_path(write=False))
        if name == "pc_media":
            return pc.media(str(args.get("action") or ""), int(args.get("times") or 1))
        if name == "pc_screenshot":
            return pc.screenshot(Path(self.spec.project_dir) / "screenshots")
        if name == "pc_recycle":
            return pc.recycle(checked_path(write=True))
        if name == "pc_close_app":
            return pc.close_app(str(args.get("name") or ""))
        if name == "pc_power":
            return pc.power(str(args.get("action") or ""))
        raise DeliveryRefused(f"unknown computer action {name}")

    def tools(self) -> list[dict[str, Any]]:
        return TOOLS + (PC_TOOLS if self.spec.pc_actions else [])

    # ------------------------------------------------------------------ protocol

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        if mid is None:  # notification
            return None
        if method == "initialize":
            params = msg.get("params") or {}
            return _ok(
                mid,
                {
                    "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                },
            )
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": self.tools()})
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "deliver_file":
                    text = self.deliver_file(args)
                elif name == "list_recent_files":
                    text = self.list_recent_files(args)
                elif isinstance(name, str) and name.startswith("pc_"):
                    text = self.pc(name, args)
                else:
                    return _err(mid, -32602, f"unknown tool {name}")
                return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:  # refusals and action failures go back to the model
                import subprocess

                from .pc_actions import ActionRefused

                expected = (DeliveryRefused, ActionRefused, ValueError, OSError)
                if not isinstance(e, (*expected, subprocess.SubprocessError)):
                    raise
                return _ok(
                    mid, {"content": [{"type": "text", "text": f"Refused: {e}"}], "isError": True}
                )
        return _err(mid, -32601, f"method not found: {method}")


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or argv[0] != "--policy":
        print("usage: python -m tell_my_pc.mcp_server --policy <policy.json>", file=sys.stderr)
        return 2
    server = Server(PolicySpec.load(Path(argv[1])))
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps(_err(None, -32700, "parse error")).encode() + b"\n")
            out.flush()
            continue
        if not isinstance(msg, dict):
            continue
        resp = server.handle(msg)
        if resp is not None:
            out.write(json.dumps(resp).encode("utf-8") + b"\n")
            out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
