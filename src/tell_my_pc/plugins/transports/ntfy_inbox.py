"""ntfy inbox transport: receive tasks from a private ntfy topic.

For when the phone cannot reach the computer over Tailscale. The phone publishes a JSON
message to the inbox topic; the daemon subscribes and acts on it. Replies (what Siri would
have said) go out through the notifier.

Anyone who can publish to the topic could give the computer tasks, so:

* the topic must refuse anonymous access, checked at start-up (``GET /<topic>/auth`` without
  credentials must fail, and with the token must succeed); otherwise the inbox does not start;
* every message must carry the inbox ``secret`` (not the HTTP bearer token, since messages
  are stored on the ntfy server), a timestamp within the clock skew, and a fresh nonce.

Message body::

    {"secret": "...", "ts": 1794000000, "nonce": "123456789",
     "text": "run the tests", "project": "website"}

Optional ``"cmd"``: ``task`` (default), ``continue``, ``status``, ``stop``, ``lock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import httpx

from ...store import Store
from ..api import PLUGIN_API_VERSION, IncomingTask, Notification, SinkReply, TaskSink

log = logging.getLogger("tell_my_pc.inbox")

_NONCE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_MESSAGE = 16 * 1024

Reply = Callable[[Notification], Awaitable[None]]


class InboxNotPrivate(Exception):
    pass


class NtfyInboxTransport:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "ntfy_inbox"

    def __init__(
        self,
        server: str,
        topic: str,
        token: str,
        secret: str,
        store: Store,
        reply: Reply | None = None,
        max_clock_skew_s: int = 300,
        http: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("the ntfy inbox needs an access token")
        if not secret or len(secret) < 32:
            raise ValueError("the ntfy inbox secret must be at least 32 characters")
        self.server = server.rstrip("/")
        self.topic = topic
        self._token = token
        self._secret = secret.encode("utf-8")
        self.store = store
        self.reply = reply
        self.skew = max_clock_skew_s
        self._http = http
        self.sink: TaskSink | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_id: str | None = None

    # ------------------------------------------------------------------ access check

    async def verify_private(self) -> None:
        """Refuse to run on a topic that anonymous users can read."""
        url = f"{self.server}/{self.topic}/auth"
        async with httpx.AsyncClient(timeout=20, transport=self._http) as c:
            anon = await c.get(url)
            if anon.status_code == 200:
                raise InboxNotPrivate(
                    "The inbox topic accepts anonymous access, so anyone could send your "
                    "computer tasks. Reserve it or enable auth (docs/setup/ntfy.md)."
                )
            auth = await c.get(url, headers=self._auth())
            if auth.status_code != 200:
                raise InboxNotPrivate(
                    f"The inbox token cannot read the topic (HTTP {auth.status_code})."
                )

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------ messages

    def check(self, msg: dict[str, Any]) -> str | None:
        """Why a message must be ignored, or None if it may be acted on."""
        secret = msg.get("secret")
        if not isinstance(secret, str) or not hmac.compare_digest(
            secret.encode("utf-8"), self._secret
        ):
            return "bad secret"
        try:
            ts = float(msg.get("ts", ""))
        except (TypeError, ValueError):
            return "missing timestamp"
        if abs(time.time() - ts) > self.skew:
            return "timestamp outside the allowed clock skew"
        nonce = str(msg.get("nonce", ""))
        if not _NONCE_RE.match(nonce):
            return "missing or malformed nonce"
        if not self.store.use_nonce(f"inbox:{nonce}", self.skew):
            return "replayed nonce"
        return None

    async def handle(self, body: str) -> SinkReply | None:
        """Act on one published message body. Returns the reply, or None if ignored."""
        if len(body) > MAX_MESSAGE:
            self.store.audit("inbox_rejected", {"why": "too large"})
            return None
        try:
            msg = json.loads(body)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            self.store.audit("inbox_rejected", {"why": "not a JSON object"})
            return None
        why = self.check(msg)
        if why:
            self.store.audit("inbox_rejected", {"why": why})
            return None
        if self.sink is None:
            return None
        cmd = str(msg.get("cmd") or "task")
        raw_text = msg.get("text")
        text: str = raw_text if isinstance(raw_text, str) else ""
        project = msg.get("project") if isinstance(msg.get("project"), str) else None
        if cmd in ("task", "continue"):
            tier = msg.get("tier") if msg.get("tier") in ("read", "work") else None
            r = await self.sink.submit(
                IncomingTask(
                    text=text.strip(),
                    project=project,
                    tier=tier,
                    resume=cmd == "continue" or bool(msg.get("continue")),
                    source=self.name,
                )
            )
        elif cmd == "status":
            r = await self.sink.status(msg.get("id") if isinstance(msg.get("id"), str) else None)
        elif cmd == "stop":
            r = await self.sink.stop(None, project)
        elif cmd == "lock":
            r = await self.sink.lock("locked through the ntfy inbox")
        else:
            self.store.audit("inbox_rejected", {"why": f"unknown cmd {cmd!r}"})
            return None
        if self.reply:
            try:
                await self.reply(
                    Notification(title="tell-my-pc", message=r.speak, kind="info", tags=["inbox"])
                )
            except Exception as e:
                log.warning("inbox reply failed: %s", e)
        return r

    # ------------------------------------------------------------------ subscription

    async def consume_once(self) -> None:
        """One streaming connection: handle messages until the server closes it."""
        params = {"since": self._last_id} if self._last_id else {}
        timeout = httpx.Timeout(20, read=90)  # ntfy sends keepalives every ~45 s
        async with (
            httpx.AsyncClient(timeout=timeout, transport=self._http) as c,
            c.stream(
                "GET", f"{self.server}/{self.topic}/json", params=params, headers=self._auth()
            ) as resp,
        ):
            if resp.status_code != 200:
                raise RuntimeError(f"ntfy subscribe returned HTTP {resp.status_code}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("event") != "message":
                    continue
                self._last_id = str(ev.get("id") or self._last_id or "")
                await self.handle(str(ev.get("message") or ""))

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self.consume_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ntfy inbox disconnected: %s (retrying in %.0f s)", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def start(self, sink: TaskSink) -> None:
        await self.verify_private()
        self.sink = sink
        self._task = asyncio.create_task(self._run(), name="tell-my-pc-ntfy-inbox")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
