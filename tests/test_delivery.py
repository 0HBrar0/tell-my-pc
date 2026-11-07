"""Phase 4: files to the phone. Done criterion: "send me the latest screenshot" produces a
working link that expires."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tell_my_pc import delivery as D
from tell_my_pc.config import DEFAULT_WORK_COMMANDS, Config
from tell_my_pc.daemon import Daemon
from tell_my_pc.mcp_server import Server
from tell_my_pc.plugins.api import IncomingTask, Notification
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.plugins.notifiers.ntfy import NtfyNotifier
from tell_my_pc.plugins.transports.tailscale_http import TailscaleHttpTransport
from tell_my_pc.policy import DELIVER_TOOL, RECENT_FILES_TOOL, PolicySpec, build_spec, decide
from tell_my_pc.store import Store

from .conftest import TOKEN
from .helpers import RecordingNotifier

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000


@pytest.fixture
def shots(tmp_path: Path) -> Path:
    d = tmp_path / "Screenshots"
    d.mkdir()
    for i, name in enumerate(["old.png", "newer.png", "latest.png"]):
        p = d / name
        p.write_bytes(PNG + bytes([i]))
        os.utime(p, (time.time() - 100 + i * 10, time.time() - 100 + i * 10))
    return d


def spec_for(repo: Path, shots: Path, tmp_path: Path, **kw: Any) -> PolicySpec:
    db = tmp_path / "state.db"
    Store(db).close()
    return build_spec(
        tier="work",
        project_dir=str(repo),
        task_id="t1",
        work_commands=DEFAULT_WORK_COMMANDS,
        extra_read_roots=[],
        builtin_never=True,
        extra_never_paths=[],
        extra_never_programs=[],
        protect_dirs=[],
        state_dir=str(tmp_path / "state"),
        db_path=str(db),
        delivery_roots=[str(shots)],
        **kw,
    )


# ------------------------------------------------------------------ path checks


def test_check_file_allows_project_and_roots(repo: Path, shots: Path, tmp_path: Path) -> None:
    s = spec_for(repo, shots, tmp_path)
    assert D.check_file(s, "README.md").size > 0
    assert D.check_file(s, str(shots / "latest.png")).path.name == "latest.png"


@pytest.mark.parametrize(
    ("make", "needle"),
    [
        (lambda r, t: r / ".env", "Never rule"),
        (lambda r, t: t / "elsewhere.txt", "outside"),
        (lambda r, t: r, "not a file"),
        (lambda r, t: r / "missing.txt", "not a file"),
    ],
)
def test_check_file_refusals(repo: Path, shots: Path, tmp_path: Path, make, needle: str) -> None:
    (repo / ".env").write_text("SECRET=1")
    (tmp_path / "elsewhere.txt").write_text("x")
    s = spec_for(repo, shots, tmp_path)
    with pytest.raises(D.DeliveryRefused, match=needle):
        D.check_file(s, str(make(repo, tmp_path)))


def test_check_file_size_limit(repo: Path, shots: Path, tmp_path: Path) -> None:
    s = spec_for(repo, shots, tmp_path, delivery_max_bytes=100)
    with pytest.raises(D.DeliveryRefused, match="larger"):
        D.check_file(s, str(shots / "latest.png"))


def test_check_file_symlink_out_refused(repo: Path, shots: Path, tmp_path: Path) -> None:
    secret = tmp_path / "private.txt"
    secret.write_text("private")
    try:
        os.symlink(secret, repo / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    with pytest.raises(D.DeliveryRefused, match="outside"):
        D.check_file(spec_for(repo, shots, tmp_path), "link.txt")


def test_hook_rules_for_delivery(repo: Path, shots: Path, tmp_path: Path) -> None:
    s = spec_for(repo, shots, tmp_path)
    assert decide(s, DELIVER_TOOL, {"path": ".env"}).verdict == "deny"
    assert decide(s, DELIVER_TOOL, {"path": str(tmp_path / "x")}).verdict == "ask"
    assert decide(s, DELIVER_TOOL, {"path": str(shots / "latest.png")}).verdict == "allow"
    assert decide(s, RECENT_FILES_TOOL, {}).verdict == "allow"
    # delivery roots are readable, so Glob/Read can find screenshots
    assert decide(s, "Read", {"file_path": str(shots / "latest.png")}).verdict == "allow"


# ------------------------------------------------------------------ signing


def test_sign_and_verify(tmp_path: Path) -> None:
    key = D.load_key(tmp_path)
    assert D.load_key(tmp_path) == key and len(key) == 32
    exp = int(time.time()) + 60
    sig = D.sign(key, "abcd", exp)
    assert D.verify(key, "abcd", str(exp), sig)
    assert not D.verify(key, "abce", str(exp), sig)
    assert not D.verify(key, "abcd", str(exp + 1), sig)
    assert not D.verify(key, "abcd", str(exp), sig, now=exp + 1)
    assert not D.verify(D.load_key(tmp_path / "other"), "abcd", str(exp), sig)


# ------------------------------------------------------------------ MCP server


def test_mcp_server_over_stdio(repo: Path, shots: Path, tmp_path: Path) -> None:
    s = spec_for(repo, shots, tmp_path)
    pol = tmp_path / "policy.json"
    s.save(pol)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_recent_files", "arguments": {"pattern": "*.png"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "deliver_file", "arguments": {"path": str(shots / "latest.png")}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "deliver_file", "arguments": {"path": "~/.ssh/id_rsa"}}},
    ]  # fmt: skip
    r = subprocess.run(
        [sys.executable, "-m", "tell_my_pc.mcp_server", "--policy", str(pol)],
        input="\n".join(json.dumps(m) for m in msgs) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = {m["id"]: m for m in map(json.loads, r.stdout.splitlines())}
    assert out[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in out[2]["result"]["tools"]} == {"deliver_file", "list_recent_files"}
    recent = out[3]["result"]["content"][0]["text"]
    assert recent.index("latest.png") < recent.index("newer.png") < recent.index("old.png")
    assert (
        not out[4]["result"]["isError"]
        and "Queued latest.png" in out[4]["result"]["content"][0]["text"]
    )
    assert out[5]["result"]["isError"]
    store = Store(s.db_path or "")
    rows = store.unnotified_deliveries()
    store.close()
    assert [Path(r["path"]).name for r in rows] == ["latest.png"]


def test_mcp_refuses_when_locked(repo: Path, shots: Path, tmp_path: Path) -> None:
    s = spec_for(repo, shots, tmp_path)
    st = Store(s.db_path or "")
    st.set_locked(True)
    st.close()
    with pytest.raises(D.DeliveryRefused, match="locked"):
        Server(s).deliver_file({"path": "README.md"})


# ------------------------------------------------------------------ end to end


@pytest.fixture
async def system(cfg_dict: dict, tmp_path: Path, shots: Path) -> AsyncIterator[Any]:
    made: list[tuple[Daemon, TailscaleHttpTransport]] = []

    async def factory(script: list[dict[str, Any]], **delivery: Any):
        data = tmp_path / "data"
        store = Store(data / "state.db")
        t = TailscaleHttpTransport(
            "127.0.0.1", 0, TOKEN, store, 300, 600, file_key=D.load_key(data)
        )
        await t.start(None)  # type: ignore[arg-type]
        cfg_dict["server"] = {"bind": "127.0.0.1", "port": 0,
                              "public_url": f"http://127.0.0.1:{t.port}"}  # fmt: skip
        cfg_dict["delivery"] = {"roots": [str(shots)], **delivery}
        d = Daemon(
            Config.model_validate(cfg_dict), store, FakeExecutor(script), RecordingNotifier(), data
        )
        t.sink = d
        await d.start()
        made.append((d, t))
        return d, t

    yield factory
    for d, t in made:
        await d.close()
        await t.close()
        d.store.close()


async def wait_file_note(d: Daemon) -> Notification:
    assert isinstance(d.notifier, RecordingNotifier)
    for _ in range(200):
        notes = [n for n in d.notifier.sent if n.kind == "file"]
        if notes:
            return notes[0]
        await __import__("asyncio").sleep(0.05)
    raise TimeoutError("no file notification")


async def test_send_me_the_latest_screenshot(system, shots: Path, monkeypatch) -> None:
    d, _ = await system(
        [
            {"tool": RECENT_FILES_TOOL, "input": {"pattern": "*.png"}},
            {"tool": DELIVER_TOOL, "input": {"path": str(shots / "latest.png"),
                                             "note": "Your latest screenshot"}},
            {"say": "Sent your latest screenshot."},
        ],
        attach_max_mb=0,  # force a link
        link_ttl_s=60,
    )  # fmt: skip
    await d.submit(IncomingTask("send me the latest screenshot"))
    note = await wait_file_note(d)
    assert note.click and "/files/" in note.click and note.attach_path is None
    assert "Your latest screenshot" in note.message
    async with httpx.AsyncClient() as c:
        r = await c.get(note.click)
        assert r.status_code == 200 and r.content == (shots / "latest.png").read_bytes()
        assert "latest.png" in r.headers.get("content-disposition", "")
        bad = note.click.replace("sig=", "sig=0")
        assert (await c.get(bad)).status_code == 410
        # an hour later the same link has expired
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 3600)
        assert (await c.get(note.click)).status_code == 410
    await d.wait_idle()


async def test_small_files_are_attached(system, shots: Path) -> None:
    d, _ = await system([{"tool": DELIVER_TOOL, "input": {"path": str(shots / "old.png")}}])
    await d.submit(IncomingTask("send the old screenshot"))
    note = await wait_file_note(d)
    assert note.attach_path and Path(note.attach_path).name == "old.png"
    assert note.click  # a link as well, in case the attachment can't be shown
    await d.wait_idle()


async def test_never_file_not_sent(system, repo: Path) -> None:
    (repo / ".env").write_text("SECRET=1")
    d, _ = await system([{"tool": DELIVER_TOOL, "input": {"path": ".env"}}])
    await d.submit(IncomingTask("send me the env file"))
    await d.wait_idle()
    assert isinstance(d.notifier, RecordingNotifier)
    assert not [n for n in d.notifier.sent if n.kind == "file"]


async def test_ntfy_attachment_upload(tmp_path: Path) -> None:
    f = tmp_path / "shot é.png"
    f.write_bytes(PNG)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={})

    n = NtfyNotifier("https://ntfy.example.com", "topic-x", "tk", httpx.MockTransport(handler))
    await n.send(Notification("web: shot.png", "Your shot", attach_path=str(f), click="https://x"))
    req = seen[0]
    assert req.method == "PUT" and req.url.path == "/topic-x"
    assert req.content == PNG and req.headers["filename"] == "shot _.png"
    assert req.url.params["message"] == "Your shot" and req.url.params["click"] == "https://x"
    assert req.headers["authorization"] == "Bearer tk"
