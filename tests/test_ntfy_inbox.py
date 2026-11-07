from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tell_my_pc import config as C
from tell_my_pc.plugins.api import IncomingTask, Notification, SinkReply
from tell_my_pc.plugins.transports.ntfy_inbox import InboxNotPrivate, NtfyInboxTransport
from tell_my_pc.store import Store

SECRET = "s" * 40
NTFY_TOKEN = "tk_inbox_token"
TOPIC = "inbox-0123456789abcdefghijkl"


class Sink:
    def __init__(self) -> None:
        self.tasks: list[IncomingTask] = []
        self.locked = False

    async def submit(self, task: IncomingTask) -> SinkReply:
        self.tasks.append(task)
        return SinkReply(True, "Got it.")

    async def status(self, task_id: str | None = None) -> SinkReply:
        return SinkReply(True, "All quiet.")

    async def stop(self, task_id: str | None = None, project: str | None = None) -> SinkReply:
        return SinkReply(True, "Stopping.")

    async def answer_approval(self, approval_id: str, approve: bool, token: str) -> SinkReply:
        return SinkReply(False, "no")

    async def lock(self, reason: str = "") -> SinkReply:
        self.locked = True
        return SinkReply(True, "Locked.")


def msg(**kw: Any) -> str:
    body = {"secret": SECRET, "ts": time.time(), "nonce": secrets.token_hex(8), **kw}
    return json.dumps(body)


def fake_ntfy(public: bool = False, stream: list[str] | None = None) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        authed = req.headers.get("authorization") == f"Bearer {NTFY_TOKEN}"
        if req.url.path == f"/{TOPIC}/auth":
            return httpx.Response(200 if (public or authed) else 403, json={})
        if req.url.path == f"/{TOPIC}/json":
            if not authed:
                return httpx.Response(403)
            lines = [json.dumps({"event": "open"})] + [
                json.dumps({"id": f"m{i}", "event": "message", "topic": TOPIC, "message": b})
                for i, b in enumerate(stream or [])
            ]
            return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "s.db")
    yield s
    s.close()


def make(store: Store, **kw: Any) -> tuple[NtfyInboxTransport, list[Notification]]:
    replies: list[Notification] = []

    async def reply(n: Notification) -> None:
        replies.append(n)

    t = NtfyInboxTransport(
        "https://ntfy.example.com", TOPIC, NTFY_TOKEN, SECRET, store, reply, 300, **kw
    )
    return t, replies


async def test_refuses_public_topic(store: Store) -> None:
    t, _ = make(store, http=fake_ntfy(public=True))
    with pytest.raises(InboxNotPrivate, match="anonymous"):
        await t.start(Sink())


async def test_private_topic_task_flow(store: Store) -> None:
    sink = Sink()
    stream = [
        msg(text="run the tests", project="web"),
        msg(text="hack", secret="wrong" * 10),  # bad secret: ignored
        "not json",
        msg(cmd="status"),
        msg(cmd="continue", text=""),
    ]
    t, replies = make(store, http=fake_ntfy(stream=stream))
    await t.verify_private()
    t.sink = sink
    await t.consume_once()
    assert [x.text for x in sink.tasks] == ["run the tests", ""]
    assert sink.tasks[0].project == "web" and sink.tasks[0].source == "ntfy_inbox"
    assert sink.tasks[1].resume
    assert [r.message for r in replies] == ["Got it.", "All quiet.", "Got it."]
    kinds = [a["detail"]["why"] for a in store.audit_log() if a["kind"] == "inbox_rejected"]
    assert "bad secret" in kinds and "not a JSON object" in kinds


async def test_replay_and_stale_rejected(store: Store) -> None:
    t, _ = make(store)
    t.sink = Sink()
    m = msg(text="x")
    assert await t.handle(m) is not None
    assert await t.handle(m) is None  # replay
    assert await t.handle(msg(text="x", ts=time.time() - 3600)) is None
    assert await t.handle(msg(text="x", nonce="bad nonce")) is None


async def test_lock_through_inbox(store: Store) -> None:
    t, _ = make(store)
    sink = Sink()
    t.sink = sink
    await t.handle(msg(cmd="lock"))
    assert sink.locked


def test_weak_secret_or_no_token_refused(store: Store) -> None:
    with pytest.raises(ValueError):
        NtfyInboxTransport("https://x", TOPIC, NTFY_TOKEN, "short", store)
    with pytest.raises(ValueError):
        NtfyInboxTransport("https://x", TOPIC, "", SECRET, store)


# ------------------------------------------------------------------ config checks


def inbox_cfg(cfg_dict: dict, **inbox: Any) -> C.Config:
    cfg_dict["inbox"] = {
        "topic": TOPIC,
        "token": {"value": NTFY_TOKEN},
        "secret": {"value": SECRET},
        **inbox,
    }
    return C.Config.model_validate(cfg_dict)


def test_inbox_config_ok(cfg_dict: dict) -> None:
    assert C.inbox_problems(inbox_cfg(cfg_dict)) == []


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"token": {"env": "NOT_SET_ANYWHERE"}}, "unauthenticated"),
        ({"secret": {"value": "short"}}, "secret"),
        ({"topic": "short-topic"}, "24"),
        ({"server": "http://ntfy.example.com"}, "https"),
    ],
)
def test_inbox_config_refusals(cfg_dict: dict, override: dict, needle: str) -> None:
    problems = C.inbox_problems(inbox_cfg(cfg_dict, **override))
    assert any(needle in p for p in problems), problems


def test_inbox_topic_must_differ_from_notifier(cfg_dict: dict) -> None:
    cfg_dict["notifier"] = {"topic": TOPIC}
    assert any("own reports" in p for p in C.inbox_problems(inbox_cfg(cfg_dict)))


def test_inbox_secret_must_differ_from_bearer(cfg_dict: dict) -> None:
    from .conftest import TOKEN

    problems = C.inbox_problems(inbox_cfg(cfg_dict, secret={"value": TOKEN}))
    assert any("differ" in p for p in problems)
