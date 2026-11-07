"""Send to a main notifier plus copies to extra ones (``also_notify``).

Approval requests go to the main notifier only: their buttons carry one-time links that
should exist in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from ..api import PLUGIN_API_VERSION, Notification, Notifier

log = logging.getLogger("tell_my_pc")


class FanoutNotifier:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "fanout"

    def __init__(self, main: Notifier | None, extras: list[Notifier]) -> None:
        self.main = main
        self.extras = extras

    async def send(self, note: Notification) -> None:
        targets = [] if note.kind == "approval" else list(self.extras)
        results = await asyncio.gather(*(t.send(note) for t in targets), return_exceptions=True)
        for t, r in zip(targets, results, strict=True):
            if isinstance(r, BaseException):
                log.warning("notifier %s failed: %s", getattr(t, "name", t), r)
        if self.main is not None:
            await self.main.send(note)  # errors propagate: approvals fail closed on them
        elif note.kind == "approval":
            raise RuntimeError("no main notifier for approval requests")
