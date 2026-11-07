"""ntfy notifier: publishes JSON to the user's own topic, with an access token if set."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from ..api import PLUGIN_API_VERSION, Notification


class NtfyNotifier:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "ntfy"

    def __init__(
        self,
        server: str,
        topic: str,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self._token = token
        self._transport = transport

    def payload(self, note: Notification) -> dict[str, Any]:
        body: dict[str, Any] = {
            "topic": self.topic,
            "title": note.title[:250],
            "message": note.message,
            "priority": max(1, min(5, note.priority)),
        }
        if note.tags:
            body["tags"] = note.tags
        if note.click:
            body["click"] = note.click
        if note.actions:
            body["actions"] = [
                {
                    "action": "http",
                    "label": a.label[:40],
                    "url": a.url,
                    "method": a.method,
                    **({"body": a.body} if a.body is not None else {}),
                    "clear": a.clear,
                }
                for a in note.actions[:3]  # ntfy allows at most 3 actions
            ]
        return body

    async def send(self, note: Notification) -> None:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
            r = await client.post(self.server + "/", json=self.payload(note), headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"ntfy returned {r.status_code}: {r.text[:200]}")
