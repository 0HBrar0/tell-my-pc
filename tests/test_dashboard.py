"""The local dashboard: guards, what it shows, and what it can change."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tell_my_pc import dashboard as D
from tell_my_pc.config import Config
from tell_my_pc.daemon import Daemon
from tell_my_pc.plugins.api import Action, Notification
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.plugins.transports.tailscale_http import TailscaleHttpTransport
from tell_my_pc.store import Store

from .conftest import TOKEN
from .helpers import RecordingNotifier, shortcut_headers


@pytest.fixture
async def dash(cfg_dict: dict, tmp_path: Path) -> AsyncIterator[tuple[D.Dashboard, Daemon, str]]:
    cfg = Config.model_validate(cfg_dict)
    store = Store(tmp_path / "data" / "state.db")
    daemon = Daemon(
        cfg, store, FakeExecutor([{"say": "ok"}]), RecordingNotifier(), tmp_path / "data"
    )
    d = D.Dashboard(daemon, store, cfg, tmp_path / "data", port=0)
    await d.start()
    yield d, daemon, f"http://127.0.0.1:{d.port}"
    await d.close()
    await daemon.close()
    store.close()


def key_headers(d: D.Dashboard) -> dict[str, str]:
    return {"X-TMP-Key": d.key.decode()}


async def get(d: D.Dashboard, url: str, path: str, **kw: Any) -> httpx.Response:
    async with httpx.AsyncClient() as c:
        return await c.get(url + path, headers=kw.pop("headers", key_headers(d)), **kw)


async def post(d: D.Dashboard, url: str, path: str, body: Any = None, **kw: Any) -> httpx.Response:
    async with httpx.AsyncClient() as c:
        return await c.post(url + path, json=body or {}, headers=kw.pop("headers", key_headers(d)))


# --------------------------------------------------------------------------- guards


def test_key_is_created_once_and_private(tmp_path: Path) -> None:
    k = D.load_key(tmp_path)
    assert len(k) >= 32 and D.load_key(tmp_path) == k
    cfg = Config.model_validate({"projects": []})
    link = D.url(cfg, k)
    assert link.startswith("http://127.0.0.1:8790/#key=")  # key in the fragment, never sent


async def test_api_needs_the_key(dash) -> None:
    d, _, url = dash
    assert (await get(d, url, "/api/overview", headers={})).status_code == 401
    assert (await get(d, url, "/api/overview", headers={"X-TMP-Key": "x" * 43})).status_code == 401
    r = await get(d, url, "/api/overview")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_other_hosts_are_refused(dash) -> None:
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 still sends its Host."""
    d, _, url = dash
    for path in ("/", "/health", "/api/overview"):
        r = await get(d, url, path, headers={**key_headers(d), "Host": "evil.example"})
        assert r.status_code == 403, path


async def test_posts_must_be_json(dash) -> None:
    d, _, url = dash
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/api/lock", content="x", headers={**key_headers(d),
                         "Content-Type": "text/plain"})  # fmt: skip
    assert r.status_code == 415


async def test_static_files_only(dash) -> None:
    d, _, url = dash
    r = await get(d, url, "/", headers={})
    assert r.status_code == 200 and "app.js" in r.text
    assert (await get(d, url, "/static/app.css", headers={})).status_code == 200
    for bad in ("/static/../dashboard.py", "/static/%2e%2e/dashboard.py", "/static/nope.js",
                "/static/fonts/OFL.txt"):  # fmt: skip
        assert (await get(d, url, bad, headers={})).status_code == 404, bad


# --------------------------------------------------------------------------- reads


async def test_overview_and_events_show_in_and_out(dash) -> None:
    d, daemon, url = dash
    s = daemon.store
    s.audit("request", {"path": "/task", "client": "100.64.0.2", "status": 202, "ok": True,
                        "reply": "Got it."})  # fmt: skip
    await daemon.notify(Notification(title="demo: done (abcd1234)", message="All good."))
    s.audit("tool_allowed", {"tool": "Read", "input": {"file_path": "a.py"}, "reason": "ok"},
            task_id="abcd1234")  # fmt: skip
    s.audit("tool_denied", {"tool": "Bash", "input": {"command": "rm x"},
                            "reason": "approval timed out, so it counts as denied: x"},
            task_id="abcd1234")  # fmt: skip
    s.audit("tool_call", {"tool": "Read"}, task_id="abcd1234")  # duplicate, hidden

    ov = (await get(d, url, "/api/overview")).json()
    assert ov["day"]["received"] == 1 and ov["day"]["sent"] == 1
    assert ov["day"]["tools"] == {"allow": 1, "asked": 1, "deny": 0}
    assert {w["dir"] for w in ov["wire"]} == {"in", "out"}

    evs = (await get(d, url, "/api/events")).json()["events"]
    kinds = [e["kind"] for e in evs]
    assert "tool_call" not in kinds
    by = {e["kind"]: e for e in evs}
    assert by["request"]["dir"] == "in" and "Got it." in by["request"]["text"]
    assert by["notification_sent"]["dir"] == "out"
    assert by["notification_sent"]["task"] == "abcd1234"
    assert by["tool_denied"]["verdict"] == "asked-deny"
    after = (await get(d, url, f"/api/events?after={evs[-1]['id']}")).json()["events"]
    assert after == []


def test_verdicts_and_names() -> None:
    def v(kind: str, reason: str) -> str | None:
        return D.verdict(kind, {"reason": reason})

    assert v("tool_allowed", "edit inside the project") == "allow"
    assert v("tool_allowed", "approved on the phone (x)") == "asked"
    assert v("tool_denied", "Never rule: x") == "deny"
    assert v("tool_denied", "denied on the phone: x") == "asked-deny"
    assert D.tool_name("mcp__tell_my_pc__pc_open_app") == "pc_open_app"
    assert D.tool_name("StructuredOutput") == "Report"


async def test_task_detail(dash) -> None:
    d, daemon, url = dash
    row = daemon.store.add_task("demo", "run the tests", "work")
    daemon.store.audit("tool_allowed", {"tool": "Bash", "input": {"command": "pytest"},
                                        "reason": "ok"}, task_id=row.id)  # fmt: skip
    body = (await get(d, url, f"/api/tasks/{row.id}")).json()
    assert body["task"]["text"] == "run the tests"
    assert [e["tool"] for e in body["events"]] == ["Bash"]
    assert (await get(d, url, "/api/tasks/../../x")).status_code in (400, 404)
    assert (await get(d, url, "/api/tasks/ffffffff")).status_code == 404


def test_config_view_hides_secrets(cfg_dict: dict) -> None:
    cfg = Config.model_validate(
        {**cfg_dict, "server": {**cfg_dict["server"], "token": {"value": TOKEN}},
         "notifier": {"type": "ntfy", "topic": "tmp-supersecrettopic123456",
                      "token": {"value": "tk_secret"}}}
    )  # fmt: skip
    text = json.dumps(D.redacted_config(cfg))
    assert TOKEN not in text and "tk_secret" not in text and "supersecrettopic" not in text
    assert '"value": "hidden"' in text


# --------------------------------------------------------------------------- actions


async def test_approve_and_deny_locally(dash) -> None:
    d, daemon, url = dash
    s = daemon.store
    aid = s.add_approval("abcd1234", "Bash", "npm install", "not on the allowlist", 300)
    r = await post(d, url, f"/api/approvals/{aid}", {"decision": "approve"})
    assert r.status_code == 200 and s.get_approval(aid)["status"] == "approved"  # type: ignore[index]
    again = await post(d, url, f"/api/approvals/{aid}", {"decision": "deny"})
    assert again.status_code == 409  # answered once only
    old = s.add_approval("abcd1234", "Bash", "x", "y", -1)  # already expired
    assert (await post(d, url, f"/api/approvals/{old}", {"decision": "approve"})).status_code == 409
    bad = await post(d, url, f"/api/approvals/{aid}", {"decision": "maybe"})
    assert bad.status_code == 400
    answers = [e for e in s.audit_log() if e["kind"] == "approval_answer"]
    assert answers and answers[-1]["detail"]["by"] == "dashboard"


async def test_lock_and_unlock(dash) -> None:
    d, daemon, url = dash
    assert (await post(d, url, "/api/lock", {"reason": "away"})).json()["ok"]
    assert daemon.store.locked()
    assert (await get(d, url, "/api/overview")).json()["locked"]
    assert (await post(d, url, "/api/unlock")).json()["ok"]
    assert daemon.store.locked() is None


# --------------------------------------------------------------------------- logging


async def test_sent_notifications_never_log_links(cfg_dict: dict, tmp_path: Path) -> None:
    cfg = Config.model_validate(cfg_dict)
    store = Store(tmp_path / "s.db")
    daemon = Daemon(cfg, store, FakeExecutor([]), RecordingNotifier(), tmp_path / "data")
    secret_link = "https://100.64.0.1:8787/files/abc?exp=1&sig=deadbeef"
    await daemon.notify(
        Notification(
            title="demo: shot.png (abcd1234)",
            message=f"Link works for 60 min: {secret_link}",
            actions=[Action("Approve", "https://x/approval/1?t=secret-token")],
        )
    )
    row = next(r for r in store.audit_log() if r["kind"] == "notification_sent")
    text = json.dumps(row)
    assert "deadbeef" not in text and "secret-token" not in text and "[link]" in text
    assert row["detail"]["buttons"] == ["Approve"]
    store.close()


async def test_transport_logs_requests_without_the_token(tmp_path: Path) -> None:
    from .test_transport import Sink

    store = Store(tmp_path / "s.db")
    t = TailscaleHttpTransport("127.0.0.1", 0, TOKEN, store, 300, rate_limit_per_minute=30)
    await t.start(Sink())
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"http://127.0.0.1:{t.port}/task", json={"text": "run the tests"},
                         headers=shortcut_headers())  # fmt: skip
            await c.get(f"http://127.0.0.1:{t.port}/health")
    finally:
        await t.close()
    rows = [r for r in store.audit_log() if r["kind"] == "request"]
    assert len(rows) == 1  # health checks are not logged
    d = rows[0]["detail"]
    assert d["path"] == "/task" and d["status"] == 202 and d["reply"] == "Got it."
    assert TOKEN not in json.dumps(rows)
    store.close()
