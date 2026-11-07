"""HTTP transport bound only to the Tailscale interface (or loopback for testing).

Every request except ``GET /health`` must carry:

* ``Authorization: Bearer <token>``
* ``X-TMP-Timestamp: <unix seconds>`` within ``max_clock_skew_s`` of the daemon's clock
* ``X-TMP-Nonce: <8-128 chars>`` never seen before inside the skew window

Requests are rate limited per client address. Every response is JSON with a ``speak`` field
the iOS Shortcut reads aloud.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ...store import Store
from ..api import PLUGIN_API_VERSION, IncomingTask, SinkReply, TaskSink

MAX_BODY = 64 * 1024
_NONCE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

Handler = Callable[[Request], Awaitable[JSONResponse]]


class RateLimiter:
    """Token bucket per key."""

    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        if len(self._buckets) > 10_000:
            self._buckets.clear()
        return True


def reply(r: SinkReply, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": r.ok, "speak": r.speak, **r.data}, status_code=status)


def fail(status: int, speak: str) -> JSONResponse:
    return JSONResponse({"ok": False, "speak": speak}, status_code=status)


class TailscaleHttpTransport:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "tailscale_http"

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        store: Store,
        max_clock_skew_s: int = 300,
        rate_limit_per_minute: int = 20,
    ) -> None:
        if not token or len(token) < 32:
            raise ValueError("bearer token must be at least 32 characters")
        self.host = host
        self.port = port
        self._token = token.encode("utf-8")
        self.store = store
        self.skew = max_clock_skew_s
        self.limiter = RateLimiter(rate_limit_per_minute)
        self.sink: TaskSink | None = None
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self.app = self._build_app()

    # ------------------------------------------------------------------ app

    def _build_app(self) -> Starlette:
        routes = [
            Route("/health", self._health, methods=["GET"]),
            Route("/task", self._guard(self._task), methods=["POST"]),
            Route("/continue", self._guard(self._continue), methods=["POST"]),
            Route("/status", self._guard(self._status), methods=["GET", "POST"]),
            Route("/stop", self._guard(self._stop), methods=["POST"]),
        ]
        return Starlette(routes=routes)

    def _guard(self, handler: Handler) -> Handler:
        async def guarded(request: Request) -> JSONResponse:
            client = request.client.host if request.client else "?"
            if not self.limiter.allow(client):
                return fail(429, "Too many requests. Wait a minute and try again.")
            problem = self._auth_problem(request)
            if problem:
                self.store.audit("auth_failed", {"client": client, "why": problem})
                return fail(401, "Not authorized.")
            try:
                return await handler(request)
            except _BadRequest as e:
                return fail(400, str(e))

        return guarded

    def _auth_problem(self, request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return "no bearer token"
        if not hmac.compare_digest(auth[7:].strip().encode("utf-8"), self._token):
            return "bad token"
        ts = request.headers.get("x-tmp-timestamp", "")
        try:
            ts_f = float(ts)
        except ValueError:
            return "missing timestamp"
        if abs(time.time() - ts_f) > self.skew:
            return "timestamp outside the allowed clock skew"
        nonce = request.headers.get("x-tmp-nonce", "")
        if not _NONCE_RE.match(nonce):
            return "missing or malformed nonce"
        if not self.store.use_nonce(nonce, self.skew):
            return "replayed nonce"
        return None

    async def _body(self, request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise _BadRequest("Request too large.")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            # The Shortcut can also send plain dictated text.
            return {"text": raw.decode("utf-8", "replace")}
        if not isinstance(data, dict):
            raise _BadRequest("Send a JSON object.")
        return data

    def _sink(self) -> TaskSink:
        if self.sink is None:
            raise _BadRequest("The daemon is starting. Try again in a moment.")
        return self.sink

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def _task(self, request: Request) -> JSONResponse:
        b = await self._body(request)
        task = IncomingTask(
            text=_str(b.get("text")),
            project=_str(b.get("project")) or None,
            tier=b.get("tier") if b.get("tier") in ("read", "work") else None,
            resume=bool(b.get("continue") or b.get("resume")),
            source=self.name,
        )
        r = await self._sink().submit(task)
        return reply(r, 202 if r.ok else 200)

    async def _continue(self, request: Request) -> JSONResponse:
        b = await self._body(request)
        task = IncomingTask(
            text=_str(b.get("text")),
            project=_str(b.get("project")) or None,
            resume=True,
            source=self.name,
        )
        r = await self._sink().submit(task)
        return reply(r, 202 if r.ok else 200)

    async def _status(self, request: Request) -> JSONResponse:
        b = await self._body(request) if request.method == "POST" else {}
        tid = _str(b.get("id")) or request.query_params.get("id") or None
        return reply(await self._sink().status(tid))

    async def _stop(self, request: Request) -> JSONResponse:
        b = await self._body(request)
        r = await self._sink().stop(_str(b.get("id")) or None, _str(b.get("project")) or None)
        return reply(r)

    # ------------------------------------------------------------------ lifecycle

    async def start(self, sink: TaskSink) -> None:
        self.sink = sink
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
            access_log=False,
            server_header=False,
        )
        self._server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(self._server.serve(), name="tell-my-pc-http")
        for _ in range(200):
            if self._server.started:
                break
            if self._serve_task.done():
                self._serve_task.result()  # raises the bind error
            await asyncio.sleep(0.05)
        if self._server.servers and self._server.servers[0].sockets:
            self.port = self._server.servers[0].sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._serve_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._serve_task, timeout=10)


class _BadRequest(Exception):
    pass


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""
