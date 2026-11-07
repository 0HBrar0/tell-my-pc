"""Versioned plugin interfaces.

Every plugin declares the ``PLUGIN_API_VERSION`` it was written against. The daemon refuses
plugins whose major version differs from its own. Until 1.0, minor bumps may break things;
they are listed in the changelog.

The four plugin kinds:

* :class:`Transport` receives tasks and approval answers and hands them to a :class:`TaskSink`.
* :class:`Notifier` pushes summaries, questions and approval requests to the user.
* :class:`Executor` runs one task and reports progress and usage.
* :class:`Delivery` turns a local file into an expiring link (Phase 4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

PLUGIN_API_VERSION = "0.1"

Tier = Literal["read", "work", "approve"]


def api_compatible(plugin_version: str, host_version: str = PLUGIN_API_VERSION) -> bool:
    """Same major version; before 1.0 the minor version must match too."""
    p_major, p_minor = (int(x) for x in plugin_version.split(".")[:2])
    h_major, h_minor = (int(x) for x in host_version.split(".")[:2])
    if h_major == 0:
        return (p_major, p_minor) == (h_major, h_minor)
    return p_major == h_major


# --------------------------------------------------------------------------- transports


@dataclass
class IncomingTask:
    text: str
    project: str | None = None
    tier: Tier | None = None
    resume: bool = False
    source: str = "unknown"


@dataclass
class SinkReply:
    """What a transport sends back to the caller. ``speak`` is short and phone-friendly."""

    ok: bool
    speak: str
    data: dict[str, Any] = field(default_factory=dict)


class TaskSink(Protocol):
    """The daemon side that transports talk to."""

    async def submit(self, task: IncomingTask) -> SinkReply: ...
    async def status(self, task_id: str | None = None) -> SinkReply: ...
    async def stop(self, task_id: str | None = None, project: str | None = None) -> SinkReply: ...


@runtime_checkable
class Transport(Protocol):
    api_version: ClassVar[str]
    name: ClassVar[str]

    async def start(self, sink: TaskSink) -> None: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- notifiers


@dataclass
class Action:
    """A button on a notification. ``url`` is called with ``method`` when tapped."""

    label: str
    url: str
    method: str = "POST"
    body: str | None = None
    clear: bool = True


@dataclass
class Notification:
    title: str
    message: str
    priority: int = 3  # 1 (min) .. 5 (max), ntfy scale
    tags: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    click: str | None = None
    kind: Literal["report", "question", "approval", "info"] = "info"


@runtime_checkable
class Notifier(Protocol):
    api_version: ClassVar[str]
    name: ClassVar[str]

    async def send(self, note: Notification) -> None: ...


# --------------------------------------------------------------------------- executors


@dataclass
class ExecRequest:
    task_id: str
    prompt: str
    cwd: str
    tier: Tier
    policy_path: str
    """JSON policy spec the PreToolUse hook enforces."""
    resume_session_id: str | None = None
    timeout_s: float = 3600
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecEvent:
    kind: Literal["started", "tool", "text", "denied", "usage", "error"]
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecResult:
    ok: bool
    session_id: str | None = None
    summary: str = ""
    structured: dict[str, Any] | None = None
    """Model-reported done / not_done / needs_from_you / errors (never files_changed)."""
    errors: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    stopped: bool = False
    limit_reset_at: float | None = None
    """Epoch seconds when a usage limit resets, if the run hit one (Phase 5)."""
    executor: str = ""


OnEvent = Callable[[ExecEvent], Awaitable[None]]


@runtime_checkable
class Executor(Protocol):
    api_version: ClassVar[str]
    name: ClassVar[str]

    async def run(self, req: ExecRequest, on_event: OnEvent) -> ExecResult: ...
    async def cancel(self, task_id: str) -> None: ...


# --------------------------------------------------------------------------- delivery


@runtime_checkable
class Delivery(Protocol):
    api_version: ClassVar[str]
    name: ClassVar[str]

    def link_for(self, path: str, ttl_s: int) -> str: ...
