"""Task reports: structured JSON, a Markdown file, and a short phone notification."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .plugins.api import Notification


@dataclass
class TaskReport:
    id: str
    project: str
    asked: str
    tier: str
    status: str  # done | failed | stopped | refused | interrupted
    summary: str = ""
    done: list[str] = field(default_factory=list)
    files_changed: list[dict[str, str]] = field(default_factory=list)
    """From git diff, never from the model."""
    errors: list[str] = field(default_factory=list)
    not_done: list[dict[str, str]] = field(default_factory=list)
    needs_from_you: list[str] = field(default_factory=list)
    branch: str | None = None
    branch_kept: bool = False
    base_ref: str | None = None
    session_id: str | None = None
    resumed: bool = False
    executor: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    notes: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    fallback_model: str | None = None
    """Set when a fallback model, not Claude, did the work."""
    held_until: float | None = None
    """Set when the task waits for Claude's usage limit to reset."""

    @property
    def done_by(self) -> str:
        if self.fallback_model:
            return f"fallback model {self.fallback_model} (not Claude)"
        return "Claude" if self.executor in ("claude_code", "") else self.executor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @property
    def duration_s(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    # ------------------------------------------------------------------ text forms

    def speak(self) -> str:
        """One or two sentences for Siri / a notification body."""
        head = {
            "done": "",
            "failed": "The task failed. ",
            "stopped": "The task was stopped. ",
            "refused": "I could not start the task. ",
            "interrupted": "The task was interrupted. ",
            "held": "",
        }.get(self.status, "")
        summary = self.summary.strip() or (self.errors[0] if self.errors else "No summary.")
        if self.fallback_model and self.status == "done":
            head = "Claude was at its usage limit, so a backup model answered. "
        n = len(self.files_changed)
        tail = ""
        if n and self.branch and self.branch_kept:
            tail = f" {n} file{'s' if n != 1 else ''} changed on {self.branch}."
        if self.needs_from_you:
            tail += " It needs something from you."
        return f"{head}{summary}{tail}".strip()

    def notification(self, click: str | None = None) -> Notification:
        tags = {
            "done": ["white_check_mark"],
            "failed": ["x"],
            "stopped": ["stop_sign"],
            "refused": ["no_entry"],
            "interrupted": ["warning"],
            "held": ["hourglass"],
        }.get(self.status, [])
        lines = [self.speak()]
        if self.not_done:
            lines.append("Not done: " + "; ".join(x["item"] for x in self.not_done[:3]))
        if self.needs_from_you:
            lines.append("Needs from you: " + "; ".join(self.needs_from_you[:3]))
        msg = "\n".join(lines)
        if len(msg.encode("utf-8")) > 3800:
            msg = msg.encode("utf-8")[:3790].decode("utf-8", "ignore") + "…"
        return Notification(
            title=f"{self.project}: {self.status} ({self.id})"
            + (f" via {self.fallback_model}" if self.fallback_model else ""),
            message=msg,
            priority=4 if self.status in ("failed", "refused") or self.needs_from_you else 3,
            tags=tags,
            click=click,
            kind="report",
        )

    def to_markdown(self) -> str:
        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {x}" for x in items) if items else "- (none)"

        files = (
            "\n".join(
                f"- `{f['status']}` {f.get('old_path', '') + ' → ' if f.get('old_path') else ''}"
                f"{f['path']}"
                for f in self.files_changed
            )
            if self.files_changed
            else "- (none)"
        )
        not_done = (
            "\n".join(
                f"- {x['item']}" + (f" — {x['reason']}" if x.get("reason") else "")
                for x in self.not_done
            )
            if self.not_done
            else "- (none)"
        )
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at))
        branch = self.branch or "(none, ran in place)"
        if self.branch and not self.branch_kept:
            branch += " (deleted: no changes)"
        usage = ""
        if self.usage or self.cost_usd is not None:
            usage = f"\n\n## Usage\n\n```json\n{json.dumps(self.usage, indent=2)}\n```\n"
            if self.cost_usd is not None:
                usage += f"\nCost reported by Claude Code: ${self.cost_usd:.4f}\n"
        return f"""# Task {self.id} · {self.project} · {self.status}

**Asked:** {self.asked}

| | |
|---|---|
| Tier | {self.tier} |
| Branch | {branch} |
| Started from | {self.base_ref or "-"} |
| Session | {self.session_id or "-"}{" (resumed)" if self.resumed else ""} |
| Done by | {self.done_by} |
| Started | {started} |
| Duration | {self.duration_s:.0f} s |

## Summary

{self.summary or "(no summary)"}

## Done

{bullets(self.done)}

## Files changed (from git)

{files}

## Not done

{not_done}

## Needs from you

{bullets(self.needs_from_you)}

## Errors

{bullets(self.errors)}

## Blocked by policy

{bullets(self.denied)}

## Notes

{bullets(self.notes)}{usage}
"""

    def save(self, reports_dir: Path) -> Path:
        reports_dir.mkdir(parents=True, exist_ok=True)
        md = reports_dir / f"{self.id}.md"
        md.write_text(self.to_markdown(), encoding="utf-8")
        (reports_dir / f"{self.id}.json").write_text(self.to_json(), encoding="utf-8")
        return md
