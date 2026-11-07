"""Phase 5: usage limits and fallback models.

Done criterion: a simulated limit causes the task to be held, a Read task completes on a
fallback model, and both are labelled in the report.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from tell_my_pc.config import Config
from tell_my_pc.daemon import Daemon
from tell_my_pc.plugins.api import (
    PLUGIN_API_VERSION,
    ExecRequest,
    ExecResult,
    IncomingTask,
    OnEvent,
)
from tell_my_pc.plugins.executors.claude_code import parse_limit
from tell_my_pc.plugins.executors.fallback import FallbackExecutor
from tell_my_pc.store import Store

from .conftest import git
from .helpers import RecordingNotifier

RESET = time.time() + 3600


class LimitedClaude:
    """Stands in for Claude: hits the usage limit for the first ``limited`` runs."""

    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "claude_code"

    def __init__(self, limited: int = 1) -> None:
        self.limited = limited
        self.calls: list[ExecRequest] = []

    async def cancel(self, task_id: str) -> None: ...

    async def run(self, req: ExecRequest, on_event: OnEvent) -> ExecResult:
        self.calls.append(req)
        if len(self.calls) <= self.limited:
            return ExecResult(
                ok=False,
                session_id="sess-1",
                errors=["Claude AI usage limit reached"],
                limit_reset_at=RESET,
                executor=self.name,
            )
        return ExecResult(
            ok=True,
            session_id=req.resume_session_id or "sess-2",
            summary="Claude did it.",
            structured={
                "summary": "Claude did it.",
                "done": ["work"],
                "not_done": [],
                "needs_from_you": [],
                "errors": [],
            },
            executor=self.name,
        )


def openai_server(replies: list[dict[str, Any]], seen: list[dict[str, Any]]):
    """A fake OpenAI-compatible /chat/completions endpoint replaying ``replies``."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        msg = replies[min(len(seen) - 1, len(replies) - 1)]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": msg}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return httpx.MockTransport(handler)


def call(name: str, **args: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": f"c-{name}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
        ],
    }  # fmt: skip


def final(summary: str) -> dict[str, Any]:
    report = {"summary": summary, "done": ["read"], "not_done": [], "needs_from_you": [],
              "errors": []}  # fmt: skip
    return {"role": "assistant", "content": json.dumps(report)}


FALLBACK = {"models": [{"name": "local", "base_url": "http://127.0.0.1:11434/v1",
                        "model": "qwen2.5-coder"}]}  # fmt: skip


@pytest.fixture
async def system(cfg_dict: dict, tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    made: list[Daemon] = []

    def factory(
        claude: LimitedClaude, replies: list[dict[str, Any]] | None = None, **over: Any
    ) -> tuple[Daemon, list[dict[str, Any]]]:
        d = dict(cfg_dict)
        d.update(over)
        seen: list[dict[str, Any]] = []
        daemon = Daemon(
            Config.model_validate(d),
            Store(tmp_path / "data" / "state.db"),
            claude,
            RecordingNotifier(),
            tmp_path / "data",
        )
        transport = openai_server(replies or [final("ok")], seen)
        daemon._fallback_factory = lambda prof: FallbackExecutor(prof, http=transport)
        made.append(daemon)
        return daemon, seen

    yield factory
    for d in made:
        await d.close()
        d.store.close()


def result(d: Daemon, tid: str) -> dict[str, Any]:
    t = d.store.get_task(tid)
    assert t is not None
    return t.result or {}


# ------------------------------------------------------------------ limit parsing


def test_parse_limit_forms() -> None:
    assert parse_limit("Claude AI usage limit reached|1900000000") == 1900000000.0
    t = parse_limit("You've hit your limit · resets 3pm")
    assert t is not None and 0 < t - time.time() <= 86400
    lt = time.localtime(t)
    assert (lt.tm_hour, lt.tm_min) == (15, 0)
    t2 = parse_limit("5-hour limit reached ∙ resets 4:30am")
    assert t2 is not None and time.localtime(t2).tm_min == 30
    unknown = parse_limit("usage limit reached")
    assert unknown is not None and 3500 < unknown - time.time() < 3700
    assert parse_limit("all tests pass") is None


# ------------------------------------------------------------------ hold


async def test_limit_holds_task_then_runs_it_at_reset(system, repo: Path) -> None:
    claude = LimitedClaude(limited=1)
    d, _ = system(claude)  # no fallback configured
    await d.start()
    r = await d.submit(IncomingTask("run the tests and fix the failures"))
    await d.wait_idle()
    t = d.store.get_task(r.data["id"])
    assert t and t.status == "held" and t.hold_until and t.hold_until >= RESET
    res = result(d, t.id)
    assert res["status"] == "held" and res["held_until"] >= RESET
    assert "needs Claude" in res["summary"] and "automatically at" in res["summary"]
    assert isinstance(d.notifier, RecordingNotifier)
    assert d.notifier.sent[-1].title.startswith("demo: held")

    # While Claude is limited, new tasks don't even try Claude, and say so.
    r2 = await d.submit(IncomingTask("another work task"))
    assert "usage limit until" in r2.speak and "run then" in r2.speak
    await d.wait_idle()
    assert len(claude.calls) == 1

    # Reset time passes: both run automatically, the first one resuming its session.
    d._claude_limited_until = 0
    d.store.update_task(t.id, hold_until=time.time() - 1)
    d.store.update_task(r2.data["id"], hold_until=time.time() - 1)
    d._kick.set()
    for _ in range(200):
        if d.store.get_task(t.id).status == "done":  # type: ignore[union-attr]
            break
        await __import__("asyncio").sleep(0.05)
    await d.wait_idle()
    assert d.store.get_task(t.id).status == "done"  # type: ignore[union-attr]
    assert claude.calls[1].resume_session_id == "sess-1"
    assert "waited for Claude's usage limit" in " ".join(result(d, t.id)["notes"])
    assert result(d, t.id)["fallback_model"] is None


async def test_held_tasks_can_be_stopped(system) -> None:
    d, _ = system(LimitedClaude(limited=5))
    await d.start()
    r = await d.submit(IncomingTask("work"))
    await d.wait_idle()
    assert (await d.stop()).ok
    assert d.store.get_task(r.data["id"]).status == "stopped"  # type: ignore[union-attr]


# ------------------------------------------------------------------ read tier on fallback


async def test_read_task_completes_on_fallback_model(system, repo: Path) -> None:
    replies = [
        call("read_file", path="README.md"),
        call("read_file", path="~/.ssh/id_rsa"),
        final("The README just says hello."),
    ]
    claude = LimitedClaude(limited=1)
    d, seen = system(claude, replies, fallback=FALLBACK)
    await d.start()
    r = await d.submit(IncomingTask("what does the readme say?", tier="read"))
    await d.wait_idle()
    res = result(d, r.data["id"])
    assert res["status"] == "done"
    assert res["fallback_model"] == "local (qwen2.5-coder)"
    assert res["executor"].startswith("fallback:local")
    assert "fallback model" in " ".join(res["notes"])
    # the tool calls went through the policy: README read, SSH key refused
    tool_msgs = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert "hello" in tool_msgs[0]["content"]
    assert tool_msgs[1]["content"].startswith("Refused by tell-my-pc: Never rule")
    # labelled for the user
    md = Path(d.store.get_task(r.data["id"]).report_path or "").read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "fallback model local (qwen2.5-coder) (not Claude)" in md
    note = d.notifier.sent[-1]  # type: ignore[union-attr]
    assert "via local" in note.title and note.message.startswith("Claude was at its usage limit")
    # read tier: no write tools offered to the model
    names = {t["function"]["name"] for t in seen[0]["tools"]}
    assert names == {"read_file", "list_files", "search"}


async def test_read_fallback_can_be_switched_off(system) -> None:
    d, _ = system(LimitedClaude(), fallback={**FALLBACK, "read_tier": False})
    await d.start()
    r = await d.submit(IncomingTask("question", tier="read"))
    await d.wait_idle()
    assert d.store.get_task(r.data["id"]).status == "held"  # type: ignore[union-attr]


# ------------------------------------------------------------------ work tier


async def test_work_task_needs_project_opt_in(system) -> None:
    d, seen = system(LimitedClaude(), fallback=FALLBACK)
    await d.start()
    r = await d.submit(IncomingTask("fix the bug"))
    await d.wait_idle()
    assert d.store.get_task(r.data["id"]).status == "held"  # type: ignore[union-attr]
    assert seen == []  # the fallback model was never called


async def test_work_fallback_runs_on_fallback_branch_and_never_approves(
    system, cfg_dict: dict, repo: Path
) -> None:
    projects = [{**cfg_dict["projects"][0], "fallback_work": True}]
    replies = [
        call("write_file", path="NOTES.md", content="done by fallback\n"),
        call("run_command", command="git push"),
        final("Wrote notes; could not push."),
    ]
    d, seen = system(
        LimitedClaude(),
        replies,
        fallback=FALLBACK,
        projects=projects,
        server={"bind": "127.0.0.1", "port": 0, "public_url": "http://127.0.0.1:9"},
    )
    await d.start()
    r = await d.submit(IncomingTask("write some notes and push"))
    await d.wait_idle()
    tid = r.data["id"]
    res = result(d, tid)
    assert res["status"] == "done" and res["branch"] == f"fallback/task-{tid}"
    assert res["files_changed"] == [{"status": "A", "path": "NOTES.md"}]
    assert "NOTES.md" in git(repo, "show", "--name-only", f"fallback/task-{tid}")
    assert git(repo, "branch", "--show-current") == "main"
    push = [m for m in seen[-1]["messages"] if m["role"] == "tool"][1]["content"]
    assert push.startswith("Refused by tell-my-pc") and "not enabled" in push
    # no approval request ever went to the phone on behalf of the fallback model
    assert not [n for n in d.notifier.sent if n.kind == "approval"]  # type: ignore[union-attr]


async def test_fallback_model_unreachable_is_reported(system, cfg_dict: dict) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    d, _ = system(LimitedClaude(), fallback=FALLBACK)
    d._fallback_factory = lambda prof: FallbackExecutor(prof, http=httpx.MockTransport(down))
    await d.start()
    r = await d.submit(IncomingTask("question", tier="read"))
    await d.wait_idle()
    res = result(d, r.data["id"])
    assert res["status"] == "failed" and "could not reach fallback model" in res["errors"][-1]


# ------------------------------------------------------------------ store migration


def test_old_database_gains_hold_columns(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT"
        " NULL, project TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL, resume INTEGER"
        " NOT NULL DEFAULT 0, status TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', session_id"
        " TEXT, branch TEXT, base_ref TEXT, result_json TEXT, report_path TEXT, error TEXT)"
    )
    c.commit()
    c.close()
    s = Store(db)
    t = s.add_task("demo", "x", "work")
    s.update_task(t.id, status="held", hold_until=1.0, attempts=1)
    assert s.release_held() == [t.id]
    assert s.get_task(t.id).status == "queued"  # type: ignore[union-attr]
    s.close()
