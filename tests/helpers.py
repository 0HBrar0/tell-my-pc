from __future__ import annotations

import secrets
import time
from typing import Any, ClassVar

from tell_my_pc.plugins.api import PLUGIN_API_VERSION, Notification

from .conftest import TOKEN


class RecordingNotifier:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "recording"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, note: Notification) -> None:
        self.sent.append(note)


def shortcut_headers(token: str = TOKEN, ts: float | None = None, nonce: str | None = None):
    """Exactly what the iOS Shortcut sends."""
    return {
        "Authorization": f"Bearer {token}",
        "X-TMP-Timestamp": str(int(ts if ts is not None else time.time())),
        "X-TMP-Nonce": nonce or secrets.token_hex(8),
        "Content-Type": "application/json",
    }


def body(**kw: Any) -> dict[str, Any]:
    return kw
