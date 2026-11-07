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
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                if name == "deliver_file":
                    text = self.deliver_file(args)
                elif name == "list_recent_files":
                    text = self.list_recent_files(args)
                else:
                    return _err(mid, -32602, f"unknown tool {name}")
                return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except DeliveryRefused as e:
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
