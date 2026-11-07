"""A minimal tell-my-pc notifier plugin.

It does not import tell-my-pc at all: a plugin only has to match the documented shape.

Config::

    also_notify:
      - type: file
        options:
          path: ~/tell-my-pc-notifications.jsonl   # optional
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, ClassVar


class FileNotifier:
    api_version: ClassVar[str] = "0.2"
    name: ClassVar[str] = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def send(self, note: Any) -> None:
        record = asdict(note) if is_dataclass(note) and not isinstance(note, type) else dict(note)
        record["ts"] = time.time()
        line = json.dumps(record, default=str) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)


def create(options: dict[str, Any], ctx: Any) -> FileNotifier:
    """Entry point. ``ctx.data_dir`` is a private folder tell-my-pc gives this plugin."""
    raw = options.get("path")
    path = Path(str(raw)).expanduser() if raw else Path(ctx.data_dir) / "notifications.jsonl"
    return FileNotifier(path)
