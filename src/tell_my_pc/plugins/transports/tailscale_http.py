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
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from ... import delivery
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
        file_key: bytes | None = None,
    ) -> None:
        if not token or len(token) < 32:
            raise ValueError("bearer token must be at least 32 characters")
        self.host = host
        self.port = port
        self._token = token.encode("utf-8")
        self.store = store
        self.skew = max_clock_skew_s
        self.limiter = RateLimiter(rate_limit_per_minute)
        self.file_key = file_key
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
            Route("/lock", self._guard(self._lock), methods=["POST"]),
            # Approve/Deny buttons: authorised by a one-time capability token in the URL,
            # not the bearer token (which must never be stored on the ntfy server).
            Route("/approval/{aid}", self._approval, methods=["POST"]),
            # File links: authorised by an HMAC signature with an expiry (Phase 4).
            Route("/files/{did}", self._file, methods=["GET"]),
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
                resp = await handler(request)
            except _BadRequest as e:
                resp = fail(400, str(e))
            self._log_request(request.url.path, client, resp)
            return resp

        return guarded

    def _log_request(self, path: str, client: str, resp: Response) -> None:
        """What came in and what was said back (for the dashboard). Never the token."""
        try:
            body = json.loads(bytes(resp.body))
        except (ValueError, TypeError):
            body = {}
        self.store.audit(
            "request",
            {
                "path": path,
                "client": client,
                "status": resp.status_code,
                "ok": bool(body.get("ok")),
                "reply": str(body.get("speak") or body.get("error") or "")[:300],
            },
        )

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

    async def _lock(self, request: Request) -> JSONResponse:
        b = await self._body(request)
        return reply(await self._sink().lock(_str(b.get("reason")) or "locked from the phone"))

    async def _approval(self, request: Request) -> JSONResponse:
        client = request.client.host if request.client else "?"
        if not self.limiter.allow(client):
            return fail(429, "Too many requests.")
        aid = str(request.path_params.get("aid", ""))
        decision = request.query_params.get("d", "")
        token = request.query_params.get("t", "")
        if (
            decision not in ("approve", "deny")
            or not token
            or not re.fullmatch(r"[0-9a-f]{12}", aid)
        ):
            return fail(400, "Bad approval link.")
        r = await self._sink().answer_approval(aid, decision == "approve", token)
        resp = reply(r, 200 if r.ok else 410)
        self._log_request(f"/approval/{aid}?d={decision}", client, resp)
        return resp

    async def _file(self, request: Request) -> Response:
        client = request.client.host if request.client else "?"
        if not self.limiter.allow(client):
            return fail(429, "Too many requests.")
        did = str(request.path_params.get("did", ""))
        exp = request.query_params.get("exp", "")
        sig = request.query_params.get("sig", "")
        if (
            self.file_key is None
            or not re.fullmatch(r"[0-9a-f]{16}", did)
            or not delivery.verify(self.file_key, did, exp, sig)
        ):
            self.store.audit("file_link_refused", {"client": client})
            return fail(410, "This link has expired or is not valid.")
        if self.store.locked():
            return fail(423, "tell-my-pc is locked.")
        row = self.store.get_delivery(did)
        if row is None:
            return fail(410, "This link has expired or is not valid.")
        path = Path(str(row["path"]))
        # The file must still be the one that was checked (no swapped symlink).
        if not path.is_file() or Path(os.path.realpath(path)) != path:
            return fail(410, "That file is no longer available.")
        self.store.audit("file_downloaded", {"file": path.name}, task_id=str(row["task_id"]))
        return FileResponse(path, filename=path.name)

    # ------------------------------------------------------------------ lifecycle

    async def start(self, sink: TaskSink) -> None:
        self.sink = sink
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            log_config=None,  # use our logging setup; uvicorn's writes to the console
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
