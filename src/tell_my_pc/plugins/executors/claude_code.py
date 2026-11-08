"""Run a task with the user's own ``claude`` CLI in headless mode.

We never read or pass Claude credentials: the CLI uses whatever login the user set up.
We never pass ``--dangerously-skip-permissions``; containment is ``--tools``,
``--permission-mode dontAsk`` and the tell-my-pc PreToolUse hook (ADR 0002).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

from ...config import ExecutorConfig
from ...policy import DELIVER_TOOL, MCP_SERVER_NAME, PC_TOOLS, RECENT_FILES_TOOL
from ..api import PLUGIN_API_VERSION, ExecEvent, ExecRequest, ExecResult, OnEvent
from .base import REPORT_SCHEMA, coerce_report, system_prompt

TIER_TOOLS: dict[str, list[str]] = {
    "read": ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite", "Task"],
    "work": [
        "Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite", "Task",
        "Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "PowerShell",
    ],
}  # fmt: skip

FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "bypassPermissions")

_LIMIT_RE = re.compile(
    r"(usage limit|limit reached|hit your limit|rate limit|out of (extra )?usage)", re.I
)
_RESET_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_RESET_CLOCK_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", re.I
)


def find_claude(configured: str | None) -> str:
    """Return the claude executable, preferring the real .exe over the npm .cmd shim on
    Windows (cmd.exe would re-parse our arguments)."""
    path = configured or shutil.which("claude")
    if not path:
        raise FileNotFoundError(
            "Could not find the `claude` CLI. Install Claude Code and log in, or set "
            "executor.claude_path."
        )
    p = Path(path)
    if sys.platform == "win32" and p.suffix.lower() in (".cmd", ".ps1", ""):
        exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
        alt = p.with_suffix(".exe")
        if alt.exists():
            return str(alt)
    return str(p)


def find_git_bash(configured: str | None) -> str | None:
    """On Windows, Claude Code's Bash tool needs Git Bash. Find it near git so the Work tier
    can run commands even when Git is installed somewhere non-standard. (Our hook does not
    need it: it uses the exec form.)"""
    if sys.platform != "win32":
        return None
    if configured:
        return configured
    if os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
        return os.environ["CLAUDE_CODE_GIT_BASH_PATH"]
    git = shutil.which("git")
    if not git:
        return None
    # git.exe may live in Git/cmd, Git/bin, Git/mingw64/bin or Git/ucrt64/bin: walk up.
    for root in list(Path(git).resolve().parents)[:4]:
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.exists():
                return str(cand)
    return None


def hook_handler(policy_path: str, timeout_s: float = 600) -> dict[str, Any]:
    """Exec-form hook: Claude Code spawns our Python directly with argv, no shell, so no
    quoting issues and no Git Bash dependency. ``onFailure: block`` keeps it fail-closed."""
    return {
        "type": "command",
        "command": sys.executable,
        "args": ["-m", "tell_my_pc.hook", "--policy", str(Path(policy_path))],
        "onFailure": "block",
        "timeout": int(timeout_s),
    }


def build_settings(policy_path: str, approval_timeout_s: float = 300) -> dict[str, Any]:
    # The hook may wait for a phone approval, so it must outlive the approval timeout.
    handler = hook_handler(policy_path, max(600, approval_timeout_s + 120))
    return {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [handler]}]},
        "permissions": {"defaultMode": "dontAsk"},
    }


def build_mcp_config(policy_path: str) -> dict[str, Any]:
    """Our own local MCP server, the only one a task gets (``--strict-mcp-config``)."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "tell_my_pc.mcp_server", "--policy", str(Path(policy_path))],
            }
        }
    }


def build_args(
    exe: str,
    cfg: ExecutorConfig,
    req: ExecRequest,
    settings_path: str,
    sys_prompt_path: str,
    mcp_config_path: str | None = None,
) -> list[str]:
    args = [
        exe,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "dontAsk",
        "--tools", ",".join(TIER_TOOLS[req.tier if req.tier in TIER_TOOLS else "read"]),
        "--settings", settings_path,
        "--setting-sources", ",".join(cfg.setting_sources) or "user",
        "--strict-mcp-config",
        "--append-system-prompt-file", sys_prompt_path,
        "--json-schema", json.dumps(REPORT_SCHEMA),
    ]  # fmt: skip
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path]
        # The hook still decides every call; pc_* tools are denied outside computer tasks.
        allowed = [DELIVER_TOOL, RECENT_FILES_TOOL, *sorted(PC_TOOLS)]
        args += ["--allowedTools", ",".join(allowed)]
    if req.resume_session_id:
        args += ["--resume", req.resume_session_id]
    if cfg.model:
        args += ["--model", cfg.model]
    if cfg.max_turns:
        args += ["--max-turns", str(cfg.max_turns)]
    for bad in FORBIDDEN_FLAGS:
        if any(bad in a for a in args):
            raise AssertionError(f"refusing to launch claude with {bad}")
    return args


def parse_limit(text: str) -> float | None:
    """Return a reset time (epoch seconds) if ``text`` reports a usage limit."""
    if not _LIMIT_RE.search(text or ""):
        return None
    m = _RESET_EPOCH_RE.search(text)
    if m:
        return float(m.group(1))
    c = _RESET_CLOCK_RE.search(text)
    if c:
        when = _next_clock(int(c.group(1)), int(c.group(2) or 0), c.group(3), c.group(4))
        if when is not None:
            return when
    return time.time() + 3600  # unknown reset time: try again in an hour


def _next_clock(hour: int, minute: int, ampm: str | None, tz: str | None) -> float | None:
    """Next time the wall clock shows hour:minute (in ``tz`` if given, else local time)."""
    from datetime import datetime, timedelta

    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    zone = None
    if tz:
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(tz.strip())
        except Exception:
            zone = None
    now = datetime.now(zone) if zone else datetime.now().astimezone()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


class ClaudeCodeExecutor:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "claude_code"

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.cfg = cfg
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._stopped: set[str] = set()

    async def cancel(self, task_id: str) -> None:
        self._stopped.add(task_id)
        proc = self._procs.get(task_id)
        if proc and proc.returncode is None:
            _kill_tree(proc)

    async def run(self, req: ExecRequest, on_event: OnEvent) -> ExecResult:
        exe = find_claude(self.cfg.claude_path)
        state = Path(req.policy_path).parent
        settings_path = state / "claude-settings.json"
        spec_timeout = json.loads(Path(req.policy_path).read_text(encoding="utf-8")).get(
            "approval_timeout_s", 300
        )
        settings_path.write_text(
            json.dumps(build_settings(req.policy_path, float(spec_timeout))), encoding="utf-8"
        )
        sp_path = state / "system-prompt.md"
        sp_path.write_text(system_prompt(req.tier, req.branch, req.instructions), encoding="utf-8")
        mcp_path = state / "mcp.json"
        mcp_path.write_text(json.dumps(build_mcp_config(req.policy_path)), encoding="utf-8")
        args = build_args(
            exe, self.cfg, req, str(settings_path), str(sp_path), mcp_config_path=str(mcp_path)
        )

        env = dict(os.environ)
        env.update(req.env)
        env["TELL_MY_PC_POLICY"] = req.policy_path
        bash = find_git_bash(self.cfg.git_bash_path)
        if bash:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = bash

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=req.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=16 * 1024 * 1024,
            **kwargs,
        )
        self._procs[req.task_id] = proc
        result = ExecResult(ok=False, executor=self.name)
        stderr_chunks: list[bytes] = []
        try:
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(req.prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            async def pump_stderr() -> None:
                assert proc.stderr
                while chunk := await proc.stderr.read(65536):
                    if sum(len(c) for c in stderr_chunks) < 64_000:
                        stderr_chunks.append(chunk)

            err_task = asyncio.create_task(pump_stderr())
            await on_event(ExecEvent("started", {"pid": proc.pid}))
            try:
                await asyncio.wait_for(
                    self._read_stream(proc, result, on_event), timeout=req.timeout_s
                )
            except TimeoutError:
                result.errors.append(f"timed out after {int(req.timeout_s)} s")
                _kill_tree(proc)
            await proc.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(err_task, timeout=5)
        except asyncio.CancelledError:
            _kill_tree(proc)
            self._stopped.add(req.task_id)
            raise
        finally:
            self._procs.pop(req.task_id, None)

        stderr = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
        if req.task_id in self._stopped:
            self._stopped.discard(req.task_id)
            result.stopped = True
            result.ok = False
        elif proc.returncode not in (0, None) and not result.summary:
            result.errors.append(f"claude exited with code {proc.returncode}: {stderr[-800:]}")
        limit = parse_limit(" ".join([result.summary, *result.errors, stderr[-2000:]]))
        if limit and not result.ok:
            result.limit_reset_at = limit
        return result

    async def _read_stream(
        self, proc: asyncio.subprocess.Process, result: ExecResult, on_event: OnEvent
    ) -> None:
        assert proc.stdout
        while line := await proc.stdout.readline():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            await self._handle(ev, result, on_event)

    async def _handle(self, ev: dict[str, Any], result: ExecResult, on_event: OnEvent) -> None:
        kind = ev.get("type")
        if ev.get("session_id"):
            result.session_id = ev["session_id"]
        if kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    await on_event(
                        ExecEvent("tool", {"name": block.get("name"), "input": block.get("input")})
                    )
                elif block.get("type") == "text" and block.get("text"):
                    await on_event(ExecEvent("text", {"text": block["text"][:500]}))
        elif kind == "result":
            result.usage = ev.get("usage") or {}
            result.cost_usd = ev.get("total_cost_usd")
            raw = ev.get("result")
            text: str = raw if isinstance(raw, str) else ""
            structured = coerce_report(ev.get("structured_output"))
            if structured is None and text:
                structured = coerce_report(_json_from_text(text))
            result.structured = structured
            result.summary = (structured or {}).get("summary") or (text or "").strip()[:1500]
            result.ok = not ev.get("is_error") and ev.get("subtype") == "success"
            if not result.ok:
                result.errors.append(f"claude finished with {ev.get('subtype')}: {text[:500]}")
            for denial in ev.get("permission_denials") or []:
                await on_event(ExecEvent("denied", {"denial": denial}))
            await on_event(ExecEvent("usage", {"usage": result.usage, "cost": result.cost_usd}))


def _json_from_text(text: str) -> Any:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else text
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
