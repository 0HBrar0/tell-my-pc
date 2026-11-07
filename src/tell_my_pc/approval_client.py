"""Hook side of phone approvals. Filled in by the Approve tier (Phase 2)."""

from __future__ import annotations

from typing import Any

from .policy import Decision, PolicySpec


def request_approval(spec: PolicySpec, event: dict[str, Any], d: Decision) -> Decision:
    return Decision("deny", f"needs approval: {d.reason} (approvals are not available yet)")
