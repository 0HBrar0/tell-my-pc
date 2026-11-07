from __future__ import annotations

import json
from pathlib import Path

import httpx

from tell_my_pc.config import ProjectConfig
from tell_my_pc.plugins.api import Action, Notification
from tell_my_pc.plugins.notifiers.ntfy import NtfyNotifier
from tell_my_pc.report import TaskReport
from tell_my_pc.resolver import resolve


async def test_ntfy_payload_and_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    n = NtfyNotifier(
        "https://ntfy.example.com/", "topic-abc", "tk_secret", httpx.MockTransport(handler)
    )
    await n.send(
        Notification(
            "t",
            "m",
            priority=9,
            tags=["x"],
            actions=[Action("Approve", "https://pc/approve/1"), Action("Deny", "https://pc/d")],
        )
    )
    req = seen[0]
    assert req.url == "https://ntfy.example.com/"
    assert req.headers["authorization"] == "Bearer tk_secret"
    body = json.loads(req.content)
    assert body["topic"] == "topic-abc" and body["priority"] == 5
    assert [a["label"] for a in body["actions"]] == ["Approve", "Deny"]
    assert body["actions"][0]["action"] == "http"


async def test_ntfy_error_raises() -> None:
    n = NtfyNotifier(
        "https://ntfy.example.com", "t", None, httpx.MockTransport(lambda r: httpx.Response(403))
    )
    try:
        await n.send(Notification("t", "m"))
    except RuntimeError as e:
        assert "403" in str(e)
    else:
        raise AssertionError("expected an error")


def test_report_markdown_and_speak(tmp_path: Path) -> None:
    r = TaskReport(
        id="abcd1234",
        project="web",
        asked="run tests",
        tier="work",
        status="done",
        summary="Fixed 2 failing tests.",
        files_changed=[{"status": "M", "path": "a.py"}, {"status": "A", "path": "b.py"}],
        branch="tmp/task-abcd1234",
        branch_kept=True,
        not_done=[{"item": "push", "reason": "not allowed"}],
        needs_from_you=["review the branch"],
    )
    assert r.speak() == (
        "Fixed 2 failing tests. 2 files changed on tmp/task-abcd1234. It needs something from you."
    )
    md = r.save(tmp_path).read_text(encoding="utf-8")
    assert "`M` a.py" in md and "push — not allowed" in md
    assert (
        json.loads((tmp_path / "abcd1234.json").read_text())["files_changed"][1]["path"] == "b.py"
    )
    note = r.notification()
    assert note.priority == 4 and "Needs from you" in note.message


def test_resolver(tmp_path: Path) -> None:
    ps = [
        ProjectConfig(name="website", path=str(tmp_path), aliases=["web site", "homepage"]),
        ProjectConfig(name="backend api", path=str(tmp_path)),
    ]
    assert resolve(ps, "Website").project == ps[0]
    assert resolve(ps, "the homepage project").project == ps[0]
    assert resolve(ps, "websight").project == ps[0]  # fuzzy
    assert resolve(ps, "backend").project is None or resolve(ps, "backend").project == ps[1]
    assert resolve(ps, "banana").project is None
    assert resolve(ps, None, "continue what you were doing on the website project").project == ps[0]
    assert resolve(ps, None, "do something").project is None
    assert resolve(ps[:1], None, "do something").project == ps[0]
