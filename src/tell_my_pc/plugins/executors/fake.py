"""A scripted executor for tests and CI. No model, no network.

Each step is one of:

* ``{"tool": "<Name>", "input": {...}}`` — goes through the same policy hook logic as a real
  run (including approvals). If allowed, Write/Edit are applied and Bash is executed.
* ``{"say": "text"}`` — becomes part of the summary.
* ``{"sleep": seconds}`` — wait (lets tests stop a running task).
* ``{"limit": epoch_seconds}`` — pretend Claude hit a usage limit (Phase 5).
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

from ...hook import evaluate
from ...policy import DELIVER_TOOL, PC_TOOLS, PolicySpec, split_shell
from ..api import PLUGIN_API_VERSION, ExecEvent, ExecRequest, ExecResult, OnEvent

DEFAULT_SCRIPT: list[dict[str, Any]] = [
    {"tool": "Bash", "input": {"command": "pytest -q"}},
    {"say": "Ran the tests with the fake executor."},
]


class FakeExecutor:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "fake"

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.script = script if script is not None else list(DEFAULT_SCRIPT)
        self.calls: list[ExecRequest] = []
        self.decisions: list[tuple[str, str, str]] = []
        self._cancelled: set[str] = set()

    async def cancel(self, task_id: str) -> None:
        self._cancelled.add(task_id)

    async def run(self, req: ExecRequest, on_event: OnEvent) -> ExecResult:
        self.calls.append(req)
        spec = PolicySpec.load(req.policy_path)
        said: list[str] = []
        done: list[str] = []
        not_done: list[dict[str, str]] = []
        errors: list[str] = []
        await on_event(ExecEvent("started", {"fake": True}))
        for step in self.script:
            if req.task_id in self._cancelled:
                return ExecResult(ok=False, stopped=True, executor=self.name)
            if "sleep" in step:
                for _ in range(int(float(step["sleep"]) * 20)):
                    if req.task_id in self._cancelled:
                        return ExecResult(ok=False, stopped=True, executor=self.name)
                    await asyncio.sleep(0.05)
            elif "say" in step:
                said.append(str(step["say"]))
            elif "limit" in step:
                return ExecResult(
                    ok=False,
                    errors=["Claude usage limit reached"],
                    limit_reset_at=float(step["limit"]),
                    executor=self.name,
                )
            elif "tool" in step:
                tool, ti = str(step["tool"]), dict(step.get("input") or {})
                event = {"tool_name": tool, "tool_input": ti, "cwd": req.cwd}
                d = await asyncio.to_thread(evaluate, event, spec)
                self.decisions.append((tool, d.verdict, d.reason))
                await on_event(ExecEvent("tool", {"name": tool, "input": ti}))
                if not d.allowed:
                    await on_event(ExecEvent("denied", {"tool": tool, "reason": d.reason}))
                    not_done.append({"item": f"{tool} {_brief(ti)}", "reason": d.reason})
                    continue
                ok, msg = await asyncio.to_thread(_apply, tool, ti, req.cwd, spec)
                (done if ok else errors).append(msg)
        summary = " ".join(said) or "Fake run finished."
        return ExecResult(
            ok=True,
            session_id=req.resume_session_id or f"fake-session-{req.task_id}",
            summary=summary,
            structured={
                "summary": summary,
                "done": done,
                "not_done": not_done,
                "needs_from_you": [],
                "errors": errors,
            },
            usage={"input_tokens": 0, "output_tokens": 0},
            cost_usd=0.0,
            executor=self.name,
        )


def _brief(ti: dict[str, Any]) -> str:
    return str(ti.get("command") or ti.get("file_path") or ti.get("url") or "")[:80]


def _apply(tool: str, ti: dict[str, Any], cwd: str, spec: PolicySpec) -> tuple[bool, str]:
    if tool == "Write":
        p = Path(cwd, str(ti["file_path"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(ti.get("content", "")), encoding="utf-8")
        return True, f"wrote {ti['file_path']}"
    if tool == "Edit":
        p = Path(cwd, str(ti["file_path"]))
        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace(str(ti["old_string"]), str(ti["new_string"]), 1), "utf-8")
        return True, f"edited {ti['file_path']}"
    if tool in PC_TOOLS:
        # Simulated: tests must never open apps or lock the screen of the machine running them.
        return True, f"(simulated) {tool.rsplit('__', 1)[-1]} {_brief(ti) or ti}".strip()
    if tool == DELIVER_TOOL:
        from ...delivery import DeliveryRefused
        from ...mcp_server import Server

        try:
            return True, Server(spec).deliver_file(ti)
        except DeliveryRefused as e:
            return False, f"Refused: {e}"
    if tool in ("Bash", "PowerShell"):
        segments = split_shell(str(ti["command"]))
        if isinstance(segments, str):
            return False, segments
        code = 0
        out = ""
        for argv in segments:
            if argv[0] == "cd":
                cwd = str(Path(cwd, argv[1]))
                continue
            if argv[0] in ("pytest", "python"):
                argv = [sys.executable, "-m", "pytest", *argv[1:]] if argv[0] == "pytest" else argv
            r = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=600, check=False
            )
            code, out = r.returncode, (r.stdout + r.stderr)[-400:]
            if code:
                break
        last = out.strip().splitlines()[-1] if out.strip() else ""
        status = "passed" if code == 0 else f"failed (exit {code})"
        return code == 0, f"ran `{shlex.join(segments[0])}`: {status} {last}".strip()
    return True, f"{tool} ok"
