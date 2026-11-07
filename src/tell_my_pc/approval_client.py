"""Hook side of phone approvals (Approve tier).

The hook and the daemon share the SQLite state file. For an ``ask`` verdict the hook files a
pending approval and waits. The daemon sees it, pushes Approve/Deny buttons to the phone, and
records the tap. If nobody answers before the deadline the hook marks it expired: a timeout is
a deny. Any error is a deny.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .policy import Decision, PolicySpec

POLL_S = 0.25


def _detail(event: dict[str, Any]) -> str:
    ti = event.get("tool_input") or {}
    if isinstance(ti, dict):
        for key in ("command", "file_path", "notebook_path", "url", "path"):
            if ti.get(key):
                return str(ti[key])
    return json.dumps(ti, default=str)[:500]


def request_approval(spec: PolicySpec, event: dict[str, Any], d: Decision) -> Decision:
    if not spec.db_path:
        return Decision("deny", f"needs approval: {d.reason} (no state database)")
    from .store import Store

    store = Store(spec.db_path)
    try:
        if store.locked():
            return Decision("deny", "tell-my-pc is locked")
        aid = store.add_approval(
            spec.task_id,
            str(event.get("tool_name") or "?"),
            _detail(event),
            d.reason,
            spec.approval_timeout_s,
        )
        deadline = time.time() + spec.approval_timeout_s
        while time.time() < deadline:
            row = store.get_approval(aid)
            status = row["status"] if row else "denied"
            if status == "approved":
                return Decision("allow", f"approved on the phone ({d.reason})")
            if status in ("denied", "expired"):
                return Decision("deny", f"denied on the phone: {d.reason}")
            if store.locked():
                store.decide_approval(aid, "denied")
                return Decision("deny", "tell-my-pc is locked")
            time.sleep(POLL_S)
        store.decide_approval(aid, "expired")
        row = store.get_approval(aid)
        if row and row["status"] == "approved":  # answered in the last instant
            return Decision("allow", f"approved on the phone ({d.reason})")
        return Decision("deny", f"approval timed out, so it counts as denied: {d.reason}")
    finally:
        store.close()
