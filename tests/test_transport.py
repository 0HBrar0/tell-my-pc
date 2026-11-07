from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tell_my_pc.plugins.api import IncomingTask, SinkReply
from tell_my_pc.plugins.transports.tailscale_http import RateLimiter, TailscaleHttpTransport
from tell_my_pc.store import Store

from .conftest import TOKEN
from .helpers import shortcut_headers


class Sink:
    def __init__(self) -> None:
        self.got: list[IncomingTask] = []

    async def submit(self, task: IncomingTask) -> SinkReply:
        self.got.append(task)
        return SinkReply(True, "Got it.", {"id": "abcd1234"})

    async def status(self, task_id: str | None = None) -> SinkReply:
        return SinkReply(True, f"status {task_id}")

    async def stop(self, task_id: str | None = None, project: str | None = None) -> SinkReply:
        return SinkReply(True, "Stopping.")


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[tuple[TailscaleHttpTransport, Sink, str]]:
    store = Store(tmp_path / "s.db")
    t = TailscaleHttpTransport("127.0.0.1", 0, TOKEN, store, 300, rate_limit_per_minute=30)
    sink = Sink()
    await t.start(sink)
    yield t, sink, f"http://127.0.0.1:{t.port}"
    await t.close()
    store.close()


async def test_shortcut_request_accepted(server) -> None:
    _, sink, url = server
    async with httpx.AsyncClient() as c:
        r = await c.post(
            url + "/task",
            json={"text": "run the tests", "project": "demo"},
            headers=shortcut_headers(),
        )
    assert r.status_code == 202
    assert r.json()["speak"] == "Got it."
    assert sink.got[0].text == "run the tests" and sink.got[0].project == "demo"


async def test_plain_text_body(server) -> None:
    _, sink, url = server
    h = shortcut_headers()
    h["Content-Type"] = "text/plain"
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/task", content="review my project", headers=h)
    assert r.status_code == 202 and sink.got[0].text == "review my project"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-wrong-token-wrong-token-xx"},
        shortcut_headers(token="x" * 40),
        shortcut_headers(ts=time.time() - 3600),
        shortcut_headers(ts=time.time() + 3600),
        {k: v for k, v in shortcut_headers().items() if k != "X-TMP-Nonce"},
        {k: v for k, v in shortcut_headers().items() if k != "X-TMP-Timestamp"},
        shortcut_headers(nonce="bad nonce!"),
    ],
)
async def test_rejects_bad_auth(server, headers: dict[str, str]) -> None:
    _, sink, url = server
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/task", json={"text": "x"}, headers=headers)
    assert r.status_code == 401 and r.json()["speak"] == "Not authorized."
    assert sink.got == []


async def test_rejects_replayed_nonce(server) -> None:
    _, sink, url = server
    h = shortcut_headers(nonce="same-nonce-123")
    async with httpx.AsyncClient() as c:
        r1 = await c.post(url + "/task", json={"text": "x"}, headers=h)
        r2 = await c.post(url + "/task", json={"text": "x"}, headers=h)
    assert r1.status_code == 202 and r2.status_code == 401
    assert len(sink.got) == 1


async def test_rate_limit(server) -> None:
    _, _, url = server
    codes = []
    async with httpx.AsyncClient() as c:
        for _ in range(40):
            r = await c.get(url + "/status", headers=shortcut_headers())
            codes.append(r.status_code)
    assert 429 in codes and codes[0] == 200


async def test_health_needs_no_auth(server) -> None:
    _, _, url = server
    async with httpx.AsyncClient() as c:
        r = await c.get(url + "/health")
    assert r.json() == {"ok": True}


async def test_continue_status_stop(server) -> None:
    _, sink, url = server
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/continue", json={"project": "web"}, headers=shortcut_headers())
        assert r.status_code == 202 and sink.got[-1].resume
        r = await c.post(url + "/status", json={"id": "abcd1234"}, headers=shortcut_headers())
        assert r.json()["speak"] == "status abcd1234"
        r = await c.post(url + "/stop", json={}, headers=shortcut_headers())
        assert r.json()["speak"] == "Stopping."


async def test_too_large(server) -> None:
    _, _, url = server
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/task", content="x" * 70_000, headers=shortcut_headers())
    assert r.status_code == 400


def test_weak_token_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TailscaleHttpTransport("127.0.0.1", 0, "short", Store(tmp_path / "x.db"))


def test_rate_limiter_refills() -> None:
    rl = RateLimiter(2)
    assert rl.allow("a", 0) and rl.allow("a", 0) and not rl.allow("a", 0)
    assert rl.allow("a", 31)
