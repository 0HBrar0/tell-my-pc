"""Phase 2 done criterion: a risky command waits for approval, and a timeout denies it."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tell_my_pc.config import Config
from tell_my_pc.daemon import Daemon
from tell_my_pc.plugins.api import IncomingTask, Notification
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.plugins.transports.tailscale_http import TailscaleHttpTransport
from tell_my_pc.store import Store

from .conftest import TOKEN
from .helpers import RecordingNotifier, shortcut_headers

PY = Path(sys.executable).as_posix()
RISKY = f"{PY} --version"  # not on the allowlist: needs a tap on the phone


@pytest.fixture
async def setup(cfg_dict: dict, tmp_path: Path) -> AsyncIterator[Any]:
    made: list[tuple[Daemon, TailscaleHttpTransport | None]] = []

    async def factory(script: list[dict[str, Any]], timeout_s: int = 30, public: bool = True):
        d = dict(cfg_dict)
        d["policy"] = {"approval_timeout_s": timeout_s}
        store = Store(tmp_path / "data" / "state.db")
        transport = TailscaleHttpTransport("127.0.0.1", 0, TOKEN, store, 300, 600)
        await transport.start(None)  # type: ignore[arg-type]
        if public:
            d["server"] = {"bind": "127.0.0.1", "port": 0,
                           "public_url": f"http://127.0.0.1:{transport.port}"}  # fmt: skip
        cfg = Config.model_validate(d)
        ex = FakeExecutor(script)
        daemon = Daemon(cfg, store, ex, RecordingNotifier(), tmp_path / "data")
        transport.sink = daemon
        await daemon.start()
        made.append((daemon, transport))
        return daemon, ex, transport

    yield factory
    for daemon, transport in made:
        await daemon.close()
        if transport:
            await transport.close()
        daemon.store.close()


async def wait_for_approval(d: Daemon, timeout: float = 20) -> Notification:
    assert isinstance(d.notifier, RecordingNotifier)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for n in d.notifier.sent:
            if n.kind == "approval":
                return n
        await asyncio.sleep(0.05)
    raise TimeoutError("no approval notification")


async def tap(note: Notification, label: str) -> httpx.Response:
    """Do what the ntfy app does when the button is tapped: an HTTP POST, no bearer token."""
    action = next(a for a in note.actions if a.label == label)
    async with httpx.AsyncClient() as c:
        return await c.request(action.method, action.url)


async def test_risky_command_waits_and_runs_after_approve(setup) -> None:
    d, ex, _ = await setup([{"tool": "Bash", "input": {"command": RISKY}}])
    r = await d.submit(IncomingTask("run the risky thing"))
    note = await wait_for_approval(d)
    # it is waiting: the task is still running and nothing has been decided
    t = d.store.get_task(r.data["id"])
    assert t and t.status == "running" and ex.decisions == []
    assert [a.label for a in note.actions] == ["Approve", "Deny"]
    assert TOKEN not in note.actions[0].url  # the bearer token never goes to ntfy
    resp = await tap(note, "Approve")
    assert resp.status_code == 200 and resp.json()["speak"] == "Approved."
    await d.wait_idle()
    assert ex.decisions[0][1] == "allow" and "approved on the phone" in ex.decisions[0][2]
    t = d.store.get_task(r.data["id"])
    assert t and any("passed" in x for x in (t.result or {})["done"])


async def test_deny_button_blocks(setup) -> None:
    d, ex, _ = await setup([{"tool": "Bash", "input": {"command": RISKY}}])
    await d.submit(IncomingTask("run the risky thing"))
    note = await wait_for_approval(d)
    assert (await tap(note, "Deny")).json()["speak"] == "Denied."
    await d.wait_idle()
    assert ex.decisions[0][1] == "deny"


async def test_timeout_counts_as_deny(setup) -> None:
    d, ex, _ = await setup([{"tool": "Bash", "input": {"command": RISKY}}], timeout_s=10)
    t0 = time.monotonic()
    r = await d.submit(IncomingTask("run the risky thing"))
    note = await wait_for_approval(d)
    await d.wait_idle(timeout=60)
    assert time.monotonic() - t0 >= 9
    assert ex.decisions[0][1] == "deny" and "timed out" in ex.decisions[0][2]
    t = d.store.get_task(r.data["id"])
    assert t and "timed out" in (t.result or {})["not_done"][0]["reason"]
    # a late tap is refused
    resp = await tap(note, "Approve")
    assert resp.status_code == 410


async def test_reused_or_forged_link_refused(setup) -> None:
    d, ex, transport = await setup([{"tool": "Bash", "input": {"command": RISKY}}])
    await d.submit(IncomingTask("run the risky thing"))
    note = await wait_for_approval(d)
    url = urlparse(note.actions[0].url)
    aid = url.path.rsplit("/", 1)[1]
    async with httpx.AsyncClient() as c:
        forged = await c.post(f"http://127.0.0.1:{transport.port}/approval/{aid}?d=approve&t=x")
        assert forged.status_code == 410
    assert (await tap(note, "Deny")).status_code == 200
    assert (await tap(note, "Approve")).status_code == 410  # one answer only
    await d.wait_idle()
    assert ex.decisions[0][1] == "deny"
    assert parse_qs(url.query)["d"] == ["approve"]


async def test_no_public_url_means_immediate_deny(setup) -> None:
    d, ex, _ = await setup([{"tool": "Bash", "input": {"command": RISKY}}], public=False)
    await d.submit(IncomingTask("run the risky thing"))
    await d.wait_idle(timeout=20)
    assert ex.decisions[0][1] == "deny" and "not enabled" in ex.decisions[0][2]


async def test_never_rules_are_not_approvable(setup) -> None:
    d, ex, _ = await setup([{"tool": "Bash", "input": {"command": "reg delete HKCU\\X"}}])
    await d.submit(IncomingTask("delete a registry key"))
    await d.wait_idle(timeout=20)
    assert ex.decisions[0][1] == "deny"
    assert isinstance(d.notifier, RecordingNotifier)
    assert not [n for n in d.notifier.sent if n.kind == "approval"]


async def test_lock_denies_pending_approval_and_new_tasks(setup) -> None:
    d, ex, transport = await setup([{"tool": "Bash", "input": {"command": RISKY}}])
    await d.submit(IncomingTask("run the risky thing"))
    await wait_for_approval(d)
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"http://127.0.0.1:{transport.port}/lock", json={}, headers=shortcut_headers()
        )
    assert r.json()["speak"].startswith("Locked.")
    await d.wait_idle(timeout=30)
    assert not (await d.submit(IncomingTask("anything"))).ok
    assert ex.decisions == [] or ex.decisions[0][1] == "deny"
