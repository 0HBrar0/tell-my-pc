"""PreToolUse hook: the enforcement point for tiers and Never rules.

Claude Code runs this once per tool call with the event JSON on stdin::

    python -m tell_my_pc.hook --policy <task policy.json>

Allowed calls get a JSON ``permissionDecision: allow``. Denied calls exit with code 2 and the
reason on stderr, which Claude Code treats as a block whatever else is configured. Any error,
including a missing or unreadable policy, is a deny (fail closed).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .policy import WEB_TOOLS, Decision, PolicySpec, decide


def resolve_ask(spec: PolicySpec, event: dict[str, Any], d: Decision) -> Decision:
    """Turn an ``ask`` into allow/deny. Asks the daemon for a phone approval if one is
    configured; with no approval channel (or on timeout) the answer is deny."""
    if not spec.approval_url:
        return Decision("deny", f"needs approval: {d.reason} (approvals are not enabled)")
    from .approval_client import request_approval  # only imported when needed

    return request_approval(spec, event, d)


def evaluate(event: dict[str, Any], spec: PolicySpec) -> Decision:
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}
    d = decide(spec, tool, tool_input)
    if d.verdict == "ask":
        d = resolve_ask(spec, event, d)
    if d.allowed and tool in WEB_TOOLS:
        spec.mark_web_tainted()
    return d


def _audit(spec: PolicySpec | None, event: dict[str, Any], d: Decision) -> None:
    if not spec or not spec.db_path:
        return
    try:
        from .store import Store

        s = Store(spec.db_path)
        try:
            s.audit(
                "tool_allowed" if d.allowed else "tool_denied",
                {
                    "tool": event.get("tool_name"),
                    "input": _short(event.get("tool_input")),
                    "reason": d.reason,
                },
                task_id=spec.task_id or None,
            )
        finally:
            s.close()
    except Exception as e:  # auditing must never turn a deny into an allow
        print(f"tell-my-pc hook: audit failed: {e}", file=sys.stderr)


def _short(value: Any, limit: int = 500) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "…"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    policy_path = os.environ.get("TELL_MY_PC_POLICY")
    if len(argv) >= 2 and argv[0] == "--policy":
        policy_path = argv[1]
    spec: PolicySpec | None = None
    event: dict[str, Any] = {}
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        if not policy_path:
            raise RuntimeError("no policy given")
        spec = PolicySpec.load(policy_path)
        d = evaluate(event, spec)
    except Exception as e:
        d = Decision("deny", f"tell-my-pc policy hook failed closed: {e}")
    _audit(spec, event, d)
    if d.allowed:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"tell-my-pc: {d.reason}",
            }
        }
        sys.stdout.write(json.dumps(out))
        return 0
    sys.stderr.write(f"Blocked by tell-my-pc ({d.verdict}): {d.reason}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
