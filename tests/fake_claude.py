"""Stands in for the claude CLI: reads the prompt on stdin, prints stream-json events.

Behaviour is picked by the prompt text: "SLEEP" hangs, "LIMIT" reports a usage limit,
"CRASH" exits non-zero, anything else succeeds with a structured report.
"""

import json
import sys
import time

prompt = sys.stdin.read()
args = sys.argv[1:]
assert "--dangerously-skip-permissions" not in args
assert args[args.index("--permission-mode") + 1] == "dontAsk"


def emit(ev: dict) -> None:
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-new"
emit({"type": "system", "subtype": "init", "session_id": sid})
emit(
    {
        "type": "assistant",
        "session_id": sid,
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]},
    }
)
if "SLEEP" in prompt:
    time.sleep(120)
if "CRASH" in prompt:
    sys.stderr.write("boom")
    sys.exit(3)
if "LIMIT" in prompt:
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "session_id": sid,
            "result": "Claude AI usage limit reached|1900000000",
        }
    )
    sys.exit(1)
emit(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": sid,
        "result": "",
        "structured_output": {
            "summary": "All 3 tests pass.",
            "done": ["ran tests"],
            "not_done": [{"item": "push", "reason": "not allowed"}],
            "needs_from_you": [],
            "errors": [],
            "files_changed": ["model claims are ignored"],
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.01,
        "permission_denials": [],
    }
)
