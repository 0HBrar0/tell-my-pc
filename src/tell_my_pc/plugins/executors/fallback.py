"""Run a task on a fallback model when Claude is at its usage limit (Phase 5).

Any OpenAI-compatible chat endpoint works (OpenAI, Ollama, LM Studio, vLLM). The model gets
a few local tools; **every** call goes through the same policy as Claude's tool calls
(``tell_my_pc.hook.evaluate``), with approvals switched off: on a fallback model, anything
that would need a tap on the phone is simply denied. Approve-tier work never runs here.

* Read tier: ``read_file``, ``list_files``, ``search``.
* Work tier (only for projects with ``fallback_work: true``): also ``write_file`` and
  ``run_command`` (Work allowlist only).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ...config import FallbackModelConfig
from ...hook import evaluate
from ...policy import PolicySpec, split_shell
from ..api import PLUGIN_API_VERSION, ExecEvent, ExecRequest, ExecResult, OnEvent
from .base import coerce_report, system_prompt

_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


def _fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


READ_TOOLS = [
    _fn("read_file", "Read a text file.", {"path": {"type": "string"}}, ["path"]),
    _fn(
        "list_files",
        "List files matching a glob pattern such as **/*.py, newest first.",
        {"pattern": {"type": "string"}, "path": {"type": "string"}},
        ["pattern"],
    ),
    _fn(
        "search",
        "Search file contents for a regular expression. Returns matching lines.",
        {"pattern": {"type": "string"}, "path": {"type": "string"}},
        ["pattern"],
    ),
]
WORK_TOOLS = [
    _fn(
        "write_file",
        "Create or overwrite a file inside the project.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _fn(
        "run_command",
        "Run an allowlisted command (tests, builds, git status/diff/add/commit).",
        {"command": {"type": "string"}},
        ["command"],
    ),
]

# fallback tool -> (Claude tool name the policy knows, input mapper)
POLICY_NAMES = {
    "read_file": "Read",
    "list_files": "Glob",
    "search": "Grep",
    "write_file": "Write",
    "run_command": "Bash",
}

FINAL_INSTRUCTIONS = """
You are a fallback model standing in for Claude, which is at its usage limit. Be honest about
what you could not do. When you are finished, reply with ONLY a JSON object (no prose, no code
fence) with keys: summary (one or two spoken sentences), done (list), not_done (list of
{"item","reason"}), needs_from_you (list), errors (list).
"""


class FallbackExecutor:
    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "fallback"

    def __init__(
        self, profile: FallbackModelConfig, http: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.profile = profile
        self._http = http
        self._cancelled: set[str] = set()

    @property
    def label(self) -> str:
        return f"fallback:{self.profile.name} ({self.profile.model})"

    async def cancel(self, task_id: str) -> None:
        self._cancelled.add(task_id)

    async def run(self, req: ExecRequest, on_event: OnEvent) -> ExecResult:
        spec = PolicySpec.load(req.policy_path)
        spec.approvals = False  # never ask the phone on behalf of a fallback model
        tools = READ_TOOLS + (WORK_TOOLS if req.tier == "work" else [])
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(req.tier, req.branch) + FINAL_INSTRUCTIONS},
            {"role": "user", "content": req.prompt},
        ]
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        await on_event(ExecEvent("started", {"executor": self.label}))
        headers = {"Content-Type": "application/json"}
        key = self.profile.api_key.resolve() if self.profile.api_key else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = self.profile.base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(timeout=self.profile.timeout_s, transport=self._http) as c:
            for _ in range(self.profile.max_steps):
                if req.task_id in self._cancelled:
                    return ExecResult(ok=False, stopped=True, executor=self.label, usage=usage)
                try:
                    r = await c.post(
                        url,
                        headers=headers,
                        json={
                            "model": self.profile.model,
                            "messages": messages,
                            "tools": tools,
                            "temperature": 0.2,
                        },
                    )
                except httpx.HTTPError as e:
                    return self._fail(f"could not reach fallback model: {e}", usage)
                if r.status_code >= 400:
                    return self._fail(f"fallback model returned HTTP {r.status_code}", usage)
                data = r.json()
                for k in usage:
                    usage[k] += int((data.get("usage") or {}).get(k) or 0)
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                calls = msg.get("tool_calls") or []
                if not calls:
                    return self._final(str(msg.get("content") or ""), usage)
                messages.append(
                    {"role": "assistant", "content": msg.get("content"), "tool_calls": calls}
                )
                for call in calls:
                    fn = call.get("function") or {}
                    out = await self._tool(
                        str(fn.get("name")), fn.get("arguments"), req, spec, on_event
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call.get("id", ""), "content": out}
                    )
        return self._fail(f"ran out of steps ({self.profile.max_steps})", usage)

    # ------------------------------------------------------------------ results

    def _final(self, text: str, usage: dict[str, int]) -> ExecResult:
        structured = coerce_report(_json(text))
        summary = (structured or {}).get("summary") or text.strip()[:1500]
        return ExecResult(
            ok=True,
            summary=summary,
            structured=structured,
            usage=dict(usage),
            executor=self.label,
        )

    def _fail(self, why: str, usage: dict[str, int]) -> ExecResult:
        return ExecResult(ok=False, errors=[why], usage=dict(usage), executor=self.label)

    # ------------------------------------------------------------------ tools

    async def _tool(
        self, name: str, raw_args: Any, req: ExecRequest, spec: PolicySpec, on_event: OnEvent
    ) -> str:
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except (ValueError, TypeError):
            return "Error: arguments were not valid JSON."
        allowed = {t["function"]["name"] for t in READ_TOOLS}
        if req.tier == "work":
            allowed |= {t["function"]["name"] for t in WORK_TOOLS}
        if name not in allowed:
            return f"Error: no tool named {name}."
        tool, ti = _policy_input(name, args)
        await on_event(ExecEvent("tool", {"name": tool, "input": ti}))
        event = {"tool_name": tool, "tool_input": ti, "cwd": req.cwd}
        d = await asyncio.to_thread(evaluate, event, spec)
        if not d.allowed:
            await on_event(ExecEvent("denied", {"tool": tool, "reason": d.reason}))
            return f"Refused by tell-my-pc: {d.reason}"
        try:
            return await asyncio.to_thread(_run_tool, name, args, req.cwd, self.profile)
        except Exception as e:
            return f"Error: {e}"


def _policy_input(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name == "read_file":
        return "Read", {"file_path": str(args.get("path") or "")}
    if name == "list_files":
        ti: dict[str, Any] = {"pattern": str(args.get("pattern") or "*")}
        if args.get("path"):
            ti["path"] = str(args["path"])
        return "Glob", ti
    if name == "search":
        ti = {"pattern": str(args.get("pattern") or ""), "output_mode": "content"}
        if args.get("path"):
            ti["path"] = str(args["path"])
        return "Grep", ti
    if name == "write_file":
        return "Write", {"file_path": str(args.get("path") or ""), "content": args.get("content")}
    return "Bash", {"command": str(args.get("command") or "")}


def _resolve(cwd: str, p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if any(part in _SKIP for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            out.append(p)
        if len(out) > 20_000:
            break
    return out


def _run_tool(name: str, args: dict[str, Any], cwd: str, prof: FallbackModelConfig) -> str:
    limit = prof.max_file_chars
    if name == "read_file":
        text = _resolve(cwd, str(args["path"])).read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= limit else text[:limit] + "\n[truncated]"
    if name == "list_files":
        root = _resolve(cwd, str(args.get("path") or "."))
        pattern = str(args.get("pattern") or "*")
        files = [p for p in root.glob(pattern) if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return "\n".join(str(p.relative_to(root)) for p in files[:200]) or "(no files)"
    if name == "search":
        root = _resolve(cwd, str(args.get("path") or "."))
        rx = re.compile(str(args["pattern"]))
        hits: list[str] = []
        for p in [root] if root.is_file() else _walk(root):
            try:
                for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="ignore").splitlines()
                ):
                    if rx.search(line):
                        hits.append(
                            f"{p.relative_to(root) if p != root else p.name}:{i + 1}: {line[:300]}"
                        )
                        if len(hits) >= 200:
                            return "\n".join(hits)
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"
    if name == "write_file":
        p = _resolve(cwd, str(args["path"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content") or ""), encoding="utf-8")
        return f"wrote {args['path']}"
    # run_command: already allowlisted by the policy
    segments = split_shell(str(args["command"]))
    if isinstance(segments, str):
        return f"Error: {segments}"
    out, code, here = "", 0, cwd
    for argv in segments:
        if argv[0] == "cd":
            here = str(_resolve(here, argv[1]))
            continue
        r = subprocess.run(argv, cwd=here, capture_output=True, text=True, timeout=600, check=False)
        code, out = r.returncode, (r.stdout + r.stderr)
        if code:
            break
    tail = out[-limit:]
    return f"exit code {code}\n{tail}"


def _json(text: str) -> Any:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            t = t[start : end + 1]
    try:
        return json.loads(t)
    except ValueError:
        return None
