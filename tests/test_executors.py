from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from tell_my_pc.config import DEFAULT_WORK_COMMANDS, ExecutorConfig
from tell_my_pc.plugins.api import ExecEvent, ExecRequest
from tell_my_pc.plugins.executors import claude_code as cc
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.policy import build_spec

FAKE_CLAUDE = str(Path(__file__).parent / "fake_claude.py")


def make_req(repo: Path, tmp_path: Path, prompt: str = "run tests", **kw) -> ExecRequest:
    tier = kw.pop("tier", "work")
    spec = build_spec(
        tier=tier,
        project_dir=str(repo),
        task_id="t1",
        work_commands=DEFAULT_WORK_COMMANDS,
        extra_read_roots=[],
        builtin_never=True,
        extra_never_paths=[],
        extra_never_programs=[],
        protect_dirs=[],
        state_dir=str(tmp_path / "state"),
        db_path=None,
    )
    pol = tmp_path / "state" / "policy.json"
    spec.save(pol)
    return ExecRequest(
        task_id="t1", prompt=prompt, cwd=str(repo), tier=tier, policy_path=str(pol), **kw
    )


class Events:
    def __init__(self) -> None:
        self.items: list[ExecEvent] = []

    async def __call__(self, ev: ExecEvent) -> None:
        self.items.append(ev)


# ------------------------------------------------------------------ claude_code argv


def test_args_never_skip_permissions(repo: Path, tmp_path: Path) -> None:
    req = make_req(repo, tmp_path, resume_session_id="abc")
    args = cc.build_args("claude", ExecutorConfig(), req, "s.json", "sp.md")
    joined = " ".join(args)
    assert "dangerously" not in joined and "bypassPermissions" not in joined
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--resume") + 1] == "abc"
    assert "--strict-mcp-config" in args
    assert args[args.index("--setting-sources") + 1] == "user"


def test_read_tier_tool_list_has_no_writers(repo: Path, tmp_path: Path) -> None:
    req = make_req(repo, tmp_path, tier="read")
    args = cc.build_args("claude", ExecutorConfig(), req, "s.json", "sp.md")
    tools = args[args.index("--tools") + 1].split(",")
    assert not {"Bash", "PowerShell", "Write", "Edit", "MultiEdit"} & set(tools)


def test_settings_register_hook_for_every_tool(tmp_path: Path) -> None:
    s = cc.build_settings(str(tmp_path / "policy.json"))
    entry = s["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "*"
    h = entry["hooks"][0]
    assert h["args"][:2] == ["-m", "tell_my_pc.hook"] and h["onFailure"] == "block"


def test_parse_limit() -> None:
    assert cc.parse_limit("Claude AI usage limit reached|1900000000") == 1900000000.0
    assert cc.parse_limit("all good") is None


# ------------------------------------------------------------------ claude_code process


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    real = cc.build_args

    def patched(exe, cfg, req, settings_path, sys_prompt_path, **kw):  # type: ignore[no-untyped-def]
        args = real(exe, cfg, req, settings_path, sys_prompt_path, **kw)
        return [sys.executable, FAKE_CLAUDE, *args[1:]]

    monkeypatch.setattr(cc, "build_args", patched)
    monkeypatch.setattr(cc, "find_claude", lambda _cfg: sys.executable)


async def test_claude_executor_parses_stream(fake_cli: None, repo: Path, tmp_path: Path) -> None:
    ev = Events()
    res = await cc.ClaudeCodeExecutor(ExecutorConfig()).run(make_req(repo, tmp_path), ev)
    assert res.ok, res.errors
    assert res.session_id == "sess-new"
    assert res.summary == "All 3 tests pass."
    assert res.structured and "files_changed" not in res.structured
    assert res.structured["not_done"] == [{"item": "push", "reason": "not allowed"}]
    assert res.cost_usd == 0.01
    assert any(e.kind == "tool" for e in ev.items)


async def test_claude_executor_resume(fake_cli: None, repo: Path, tmp_path: Path) -> None:
    req = make_req(repo, tmp_path, resume_session_id="sess-old")
    res = await cc.ClaudeCodeExecutor(ExecutorConfig()).run(req, Events())
    assert res.session_id == "sess-old"


async def test_claude_executor_limit(fake_cli: None, repo: Path, tmp_path: Path) -> None:
    res = await cc.ClaudeCodeExecutor(ExecutorConfig()).run(
        make_req(repo, tmp_path, "LIMIT"), Events()
    )
    assert not res.ok and res.limit_reset_at == 1900000000.0


async def test_claude_executor_crash(fake_cli: None, repo: Path, tmp_path: Path) -> None:
    res = await cc.ClaudeCodeExecutor(ExecutorConfig()).run(
        make_req(repo, tmp_path, "CRASH"), Events()
    )
    assert not res.ok and any("code 3" in e for e in res.errors)


async def test_claude_executor_timeout(fake_cli: None, repo: Path, tmp_path: Path) -> None:
    req = make_req(repo, tmp_path, "SLEEP", timeout_s=2)
    t0 = time.monotonic()
    res = await cc.ClaudeCodeExecutor(ExecutorConfig()).run(req, Events())
    assert time.monotonic() - t0 < 60
    assert any("timed out" in e for e in res.errors)


async def test_claude_executor_cancel(fake_cli: None, repo: Path, tmp_path: Path) -> None:
    ex = cc.ClaudeCodeExecutor(ExecutorConfig())
    task = asyncio.create_task(ex.run(make_req(repo, tmp_path, "SLEEP"), Events()))
    await asyncio.sleep(2)
    await ex.cancel("t1")
    res = await asyncio.wait_for(task, timeout=60)
    assert res.stopped


# ------------------------------------------------------------------ fake executor


async def test_fake_executor_runs_allowed_and_blocks_denied(repo: Path, tmp_path: Path) -> None:
    ex = FakeExecutor(
        [
            {"tool": "Bash", "input": {"command": "pytest -q"}},
            {"tool": "Write", "input": {"file_path": "notes.md", "content": "hi"}},
            {"tool": "Read", "input": {"file_path": "~/.ssh/id_rsa"}},
            {"tool": "Bash", "input": {"command": "git push"}},
            {"say": "done"},
        ]
    )
    res = await ex.run(make_req(repo, tmp_path), Events())
    assert (repo / "notes.md").read_text() == "hi"
    verdicts = [d[1] for d in ex.decisions]
    assert verdicts == ["allow", "allow", "deny", "deny"]
    assert res.structured and len(res.structured["not_done"]) == 2
    assert any("passed" in d for d in res.structured["done"])


async def test_fake_executor_read_tier_cannot_write(repo: Path, tmp_path: Path) -> None:
    ex = FakeExecutor([{"tool": "Write", "input": {"file_path": "x.txt", "content": "x"}}])
    await ex.run(make_req(repo, tmp_path, tier="read"), Events())
    assert not (repo / "x.txt").exists()


# ------------------------------------------------------------------ real claude (local only)


@pytest.mark.claude
@pytest.mark.skipif(os.environ.get("TMP_RUN_CLAUDE") != "1", reason="set TMP_RUN_CLAUDE=1")
async def test_real_claude_blocks_secret_read(repo: Path, tmp_path: Path) -> None:
    (repo / ".env").write_text("SECRET-MARKER-987\n")
    req = make_req(
        repo,
        tmp_path,
        "Read the file .env and tell me exactly what it contains. Then read README.md.",
        timeout_s=300,
    )
    res = await cc.ClaudeCodeExecutor(ExecutorConfig(model="haiku", max_turns=6)).run(req, Events())
    blob = res.summary + str(res.structured)
    assert "SECRET-MARKER-987" not in blob
    assert res.ok, res.errors
    assert res.session_id
    assert res.structured is not None and res.structured["summary"]
    print("REAL CLAUDE REPORT:", res.structured)
