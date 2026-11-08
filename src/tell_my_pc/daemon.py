"""The daemon: queue, scheduler, and the glue between transport, executor and notifier."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import computer, delivery, gitops, paths
from .config import Config, FallbackModelConfig, ProjectConfig
from .plugins.api import (
    Action,
    ExecEvent,
    ExecRequest,
    ExecResult,
    Executor,
    IncomingTask,
    Notification,
    Notifier,
    OnEvent,
    SinkReply,
    Tier,
)
from .plugins.executors.fallback import FallbackExecutor
from .policy import build_spec
from .report import TaskReport
from .resolver import resolve
from .store import Store, TaskRow

log = logging.getLogger("tell_my_pc")

_CONTINUE = re.compile(r"^\s*(please\s+)?(continue|keep going|carry on|resume)\b", re.I)
MAX_TEXT = 4000
PENDING_ASK_S = 600


@dataclass
class _PendingAsk:
    """A task waiting for the user to say which project it is for."""

    text: str
    tier: Tier | None
    resume: bool
    expires_at: float


class Daemon:
    """Implements :class:`~tell_my_pc.plugins.api.TaskSink` for transports."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        executor: Executor,
        notifier: Notifier | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.executor = executor
        self.notifier = notifier
        self.data_dir = data_dir or paths.data_dir()
        self.reports_dir = self.data_dir / "reports"
        self.tasks_dir = self.data_dir / "tasks"
        self._kick = asyncio.Event()
        self._running: dict[str, asyncio.Task[None]] = {}  # task id -> asyncio task
        self._running_projects: dict[str, str] = {}  # project -> task id
        self._scheduler: asyncio.Task[None] | None = None
        self._approvals: asyncio.Task[None] | None = None
        self._pending_ask: _PendingAsk | None = None
        self._claude_limited_until = 0.0
        self._fallback_factory: Callable[[FallbackModelConfig], Executor] = lambda prof: (
            FallbackExecutor(prof)
        )
        self._idle = asyncio.Event()
        self._idle.set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        for tid in self.store.mark_interrupted():
            t = self.store.get_task(tid)
            if t:
                await self._notify(
                    Notification(
                        title=f"{t.project}: interrupted ({t.id})",
                        message="The daemon restarted while this task was running. "
                        f"Its branch tmp/task-{t.id} may hold partial work.",
                        priority=4,
                        tags=["warning"],
                    )
                )
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="tell-my-pc-scheduler")
        self._approvals = asyncio.create_task(self._approval_loop(), name="tell-my-pc-approvals")
        self._kick.set()

    async def close(self) -> None:
        for t in list(self._running.values()):
            t.cancel()
        for t in list(self._running.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for bg in (self._scheduler, self._approvals):
            if bg:
                bg.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bg

    async def wait_idle(self, timeout: float = 60) -> None:
        """Wait until nothing is queued or running (tests)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._running and not self.store.tasks(("queued",)):
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("daemon did not go idle")

    @property
    def approvals_enabled(self) -> bool:
        """Phone approvals need a notifier for the buttons and a URL the phone can call."""
        return self.notifier is not None and bool(self.cfg.server.public_url)

    # ------------------------------------------------------------------ TaskSink

    async def submit(self, task: IncomingTask) -> SinkReply:
        if self.store.locked():
            return SinkReply(False, "tell-my-pc is locked. Unlock it on the computer first.")
        text = (task.text or "").strip()
        if not text and not task.resume:
            return SinkReply(False, "I didn't catch a task. Please try again.")
        if len(text) > MAX_TEXT:
            return SinkReply(False, "That task is too long. Please keep it shorter.")
        resume = task.resume or bool(_CONTINUE.match(text))

        # Answer to an earlier "Which project?": the user just says the name.
        pending = self._pending_ask
        self._pending_ask = None
        if pending and not task.project and time.time() < pending.expires_at:
            answer = resolve(self.targets, text) if len(text.split()) <= 6 else None
            if answer and answer.project is not None:
                self.store.audit(
                    "project_answered", {"answer": text, "project": answer.project.name}
                )
                text, resume = pending.text, pending.resume
                task = IncomingTask(
                    text=text,
                    project=answer.project.name,
                    tier=pending.tier,
                    resume=resume,
                    source=task.source,
                )

        res = resolve(self.targets, task.project, text)
        if (
            res.project is None
            and not task.project
            and res.reason == "no project named"
            and self.cfg.computer.enabled
            and self.cfg.computer.default_target
        ):
            res = type(res)(computer.as_project(self.cfg.computer), [], "no project named")
        if res.project is None:
            options = ", ".join(p.name for p in res.candidates[:8])
            speak = (
                f"Which project? I know {options}. Just say the project name."
                if options
                else "No projects configured."
            )
            self._pending_ask = _PendingAsk(
                text=text,
                tier=task.tier,
                resume=resume,
                expires_at=time.time() + PENDING_ASK_S,
            )
            self.store.audit("project_asked", {"text": text[:200], "reason": res.reason})
            await self._notify(
                Notification(
                    title="tell-my-pc: which project?",
                    message=f'For "{text[:200]}": {res.reason}. Say "Tell Claude" again '
                    f"within {PENDING_ASK_S // 60} minutes with just the project name: "
                    f"{options}.",
                    tags=["question"],
                    kind="question",
                )
            )
            return SinkReply(
                False, speak, {"needs": "project", "candidates": [p.name for p in res.candidates]}
            )
        project = res.project
        tier = task.tier if task.tier in ("read", "work") else project.default_tier
        if not text:
            text = "Continue where you left off."
        row = self.store.add_task(project.name, text, tier, resume=resume, source=task.source)
        self.store.audit(
            "task_received",
            {"project": project.name, "tier": tier, "resume": resume, "source": task.source,
             "resolved": res.reason},
            task_id=row.id,
        )  # fmt: skip
        self._kick.set()
        busy = project.name in self._running_projects
        when = "It will start after the current task there." if busy else "Starting now."
        if self._claude_limited_until > time.time():
            at = time.strftime("%H:%M", time.localtime(self._claude_limited_until))
            can_fallback = bool(self.cfg.fallback.models) and (
                (tier == "read" and self.cfg.fallback.read_tier)
                or (tier == "work" and project.fallback_work)
            )
            when = (
                f"Claude is at its usage limit until {at}, so a fallback model will do it."
                if can_fallback
                else f"Claude is at its usage limit until {at}. It will run then."
            )
        return SinkReply(
            True,
            f"Got it. Task {_say_id(row.id)} for {project.name}. {when}",
            {"id": row.id, "project": project.name, "tier": tier, "resume": resume},
        )

    async def status(self, task_id: str | None = None) -> SinkReply:
        t = self.store.get_task(task_id) if task_id else self.store.latest_task()
        if t is None:
            return SinkReply(
                task_id is None, "No tasks yet." if task_id is None else "No such task."
            )
        running = [self.store.get_task(i) for i in self._running]
        data = {"task": _task_public(t), "running": [_task_public(r) for r in running if r]}
        if t.status == "running":
            mins = max(1, int((time.time() - t.created_at) / 60))
            return SinkReply(
                True, f"Task {_say_id(t.id)} on {t.project} is running, {mins} min.", data
            )
        if t.status == "queued":
            return SinkReply(
                True, f"Task {_say_id(t.id)} on {t.project} is waiting to start.", data
            )
        result = t.result or {}
        speak = result.get("speak") or f"Task {_say_id(t.id)} on {t.project} is {t.status}."
        return SinkReply(True, speak, data)

    async def stop(self, task_id: str | None = None, project: str | None = None) -> SinkReply:
        targets: list[TaskRow] = []
        if task_id:
            t = self.store.get_task(task_id)
            targets = [t] if t and t.status in ("queued", "running", "held") else []
        else:
            for t in self.store.tasks(("queued", "running", "held"), limit=500):
                if project is None or t.project.lower() == project.lower():
                    targets.append(t)
        if not targets:
            return SinkReply(True, "Nothing is running.")
        for t in targets:
            if t.status in ("queued", "held"):
                self.store.update_task(t.id, status="stopped", error="stopped before it started")
            else:
                await self.executor.cancel(t.id)
                at = self._running.get(t.id)
                if at:
                    at.cancel()
            self.store.audit("task_stop_requested", {}, task_id=t.id)
        n = len(targets)
        return SinkReply(
            True, f"Stopping {n} task{'s' if n != 1 else ''}.", {"ids": [t.id for t in targets]}
        )

    async def answer_approval(self, approval_id: str, approve: bool, token: str) -> SinkReply:
        decision = "approved" if approve else "denied"
        ok = self.store.decide_approval(approval_id, decision, token_hash=_hash(token))
        row = self.store.get_approval(approval_id) if ok else None
        self.store.audit(
            "approval_answer",
            {"approval": approval_id, "decision": decision, "accepted": ok},
            task_id=row["task_id"] if row else None,
        )
        if not ok:
            return SinkReply(False, "That approval expired or was already answered.")
        return SinkReply(True, "Approved." if approve else "Denied.")

    async def lock(self, reason: str = "") -> SinkReply:
        """Kill switch: refuse new tasks, deny every pending approval and tool call, stop
        running tasks. Unlocking is only possible on the computer (`tell-my-pc unlock`)."""
        self.store.set_locked(True, reason or "locked")
        self.store.audit("locked", {"reason": reason})
        stopped = await self.stop()
        return SinkReply(
            True, f"Locked. {stopped.speak} Unlock on the computer with tell-my-pc unlock."
        )

    # ------------------------------------------------------------------ approvals

    async def _approval_loop(self) -> None:
        """Pushes approval requests and queued files written by the hook / MCP server."""
        while True:
            try:
                for row in self.store.unnotified_approvals():
                    await self._send_approval(row)
                for row in self.store.unnotified_deliveries():
                    await self._send_file(row)
            except Exception:
                log.exception("approval/delivery loop error")
            await asyncio.sleep(0.2)

    async def _send_file(self, row: dict[str, object]) -> None:
        did = str(row["id"])
        self.store.mark_delivery_notified(did)
        path = Path(str(row["path"]))
        size = int(str(row["size"]))
        t = self.store.get_task(str(row["task_id"]))
        project = t.project if t else "tell-my-pc"
        note = str(row.get("note") or "")
        base = self.cfg.server.public_url
        url = (
            delivery.link(
                base, delivery.load_key(self.data_dir), did, float(str(row["expires_at"]))
            )
            if base
            else None
        )
        attach = size <= self.cfg.delivery.attach_max_mb * 1_000_000
        mins = max(1, round((float(str(row["expires_at"])) - time.time()) / 60))
        lines = [note] if note else []
        if url:
            lines.append(f"Link works for {mins} min: {url}")
        elif not attach:
            lines.append("Too big to attach, and no server.public_url is set for a download link.")
        await self._notify(
            Notification(
                title=f"{project}: {path.name}",
                message="\n".join(lines) or path.name,
                tags=["paperclip"],
                click=url,
                kind="file",
                attach_path=str(path) if attach else None,
            )
        )
        self.store.audit(
            "file_sent",
            {"file": path.name, "size": size, "attached": attach, "link": bool(url)},
            task_id=str(row["task_id"]),
        )

    async def _send_approval(self, row: dict[str, object]) -> None:
        aid = str(row["id"])
        token = secrets.token_urlsafe(24)
        self.store.mark_approval_notified(aid, _hash(token))
        base = (self.cfg.server.public_url or "").rstrip("/")
        if not self.notifier or not base:
            self.store.decide_approval(aid, "denied")
            return
        t = self.store.get_task(str(row["task_id"]))
        mins = max(1, round((float(str(row["expires_at"])) - time.time()) / 60))
        note = Notification(
            title=f"Approve? {t.project if t else ''} ({row['task_id']})",
            message=f"{row['tool']}: {row['detail']}\nWhy it asks: {row['reason']}\n"
            f"No answer in {mins} min counts as Deny.",
            priority=5,
            tags=["question"],
            kind="approval",
            actions=[
                Action("Approve", f"{base}/approval/{aid}?d=approve&t={token}"),
                Action("Deny", f"{base}/approval/{aid}?d=deny&t={token}"),
            ],
        )
        try:
            await self.notifier.send(note)
        except Exception as e:
            log.warning("could not send approval %s: %s", aid, e)
            self.store.decide_approval(aid, "denied")  # fail closed
        self.store.audit("approval_requested", {"approval": aid, "tool": row["tool"]},
                         task_id=str(row["task_id"]))  # fmt: skip

    # ------------------------------------------------------------------ scheduling

    async def _schedule_loop(self) -> None:
        while True:
            # Wake at least every 15 s so held tasks start when their hold time passes.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=15)
            self._kick.clear()
            try:
                self._dispatch()
            except Exception:
                log.exception("scheduler error")

    def _dispatch(self) -> None:
        for tid in self.store.release_held():
            self.store.audit("task_released", {}, task_id=tid)
        limit = self.cfg.limits.max_parallel_projects
        for t in self.store.tasks(("queued",), limit=500):
            if len(self._running) >= limit:
                break
            if t.project in self._running_projects:
                continue
            self.store.update_task(t.id, status="running")
            self._running_projects[t.project] = t.id
            self._running[t.id] = asyncio.create_task(self._run(t), name=f"task-{t.id}")

    @property
    def targets(self) -> list[ProjectConfig]:
        """Configured projects, plus the computer itself when it is enabled (ADR 0008)."""
        c = self.cfg.computer
        return [*self.cfg.projects, computer.as_project(c)] if c.enabled else self.cfg.projects

    def is_computer(self, name: str) -> bool:
        return self.cfg.computer.enabled and name == self.cfg.computer.name

    def _project(self, name: str) -> ProjectConfig:
        for p in self.targets:
            if p.name == name:
                return p
        raise KeyError(name)

    # ------------------------------------------------------------------ one task

    async def _run(self, t: TaskRow) -> None:
        report = TaskReport(id=t.id, project=t.project, asked=t.text, tier=t.tier, status="failed")
        tb: gitops.TaskBranch | None = None
        pc: computer.ComputerRun | None = None
        try:
            project = self._project(t.project)
            pdir = str(Path(project.path).expanduser().resolve())
            if self.is_computer(t.project):
                pc = await asyncio.to_thread(computer.ComputerRun.start, self.cfg.computer, t.id)
                pdir = str(pc.workspace)
                report.target = "computer"
            state_dir = self.tasks_dir / t.id
            state_dir.mkdir(parents=True, exist_ok=True)

            session = self.store.get_session(t.project) if t.resume else None
            if t.resume and not session:
                report.notes.append("No earlier session for this project, so this started fresh.")
            start_from = session.get("branch") if session else None
            if t.attempts:
                # A held task coming back: continue its own session and branch.
                report.notes.append(
                    "This task waited for Claude's usage limit to reset "
                    f"(attempt {t.attempts + 1})."
                )
                if t.session_id:
                    session = {"session_id": t.session_id, "branch": t.branch}

            if pc is None:
                try:
                    tb = await asyncio.to_thread(gitops.prepare, pdir, t.id, t.tier, start_from)
                except gitops.GitError as e:
                    report.status = "refused"
                    report.summary = str(e)
                    report.errors.append(str(e))
                    return
                if tb.note:
                    report.notes.append(tb.note)
                report.branch, report.base_ref = tb.branch, tb.base_ref
                self.store.update_task(t.id, branch=tb.branch, base_ref=tb.base_ref)

            pol = self.cfg.policy
            c = self.cfg.computer
            pc_write = [str(r) for r in pc.roots[1:]] if pc else []
            pc_read = [str(computer.expand(r)) for r in c.read_roots] if pc else []
            pc_never = [str(computer.expand(b)) + "/**" for b in c.blocked] if pc else []
            spec = build_spec(
                tier="read" if t.tier == "read" else "work",
                project_dir=pdir,
                task_id=t.id,
                work_commands=pol.work_commands,
                extra_read_roots=[str(Path(r).expanduser()) for r in pol.extra_read_roots]
                + pc_read,
                builtin_never=pol.builtin_never,
                extra_never_paths=[*pol.never_paths, *pc_never],
                extra_never_programs=pol.never_commands,
                protect_dirs=[str(paths.config_dir()), str(self.data_dir)],
                state_dir=str(state_dir),
                db_path=self.store.path if self.store.path != ":memory:" else None,
                approvals=self.approvals_enabled,
                approval_timeout_s=pol.approval_timeout_s,
                delivery_roots=[str(Path(r).expanduser()) for r in self.cfg.delivery.roots]
                + pc_write
                + pc_read,
                delivery_ttl_s=self.cfg.delivery.link_ttl_s,
                delivery_max_bytes=int(self.cfg.delivery.max_file_mb * 1_000_000),
                write_roots=pc_write,
                pc_actions=bool(pc and c.actions),
            )
            policy_path = state_dir / "policy.json"
            spec.save(policy_path)

            req = ExecRequest(
                task_id=t.id,
                prompt=t.text,
                cwd=pdir,
                tier="read" if t.tier == "read" else "work",
                policy_path=str(policy_path),
                resume_session_id=session["session_id"] if session else None,
                branch=tb.branch if tb else None,
                timeout_s=self.cfg.limits.task_timeout_s,
                instructions=computer.instructions(c, pc) if pc else "",
            )
            report.resumed = bool(req.resume_session_id)

            async def on_event(ev: ExecEvent) -> None:
                if ev.kind == "denied":
                    d = ev.data.get("denial") or ev.data
                    report.denied.append(_brief_denial(d))
                elif ev.kind == "tool":
                    self.store.audit("tool_call", {"tool": ev.data.get("name")}, task_id=t.id)

            limited_until = self._claude_limited_until
            if limited_until > time.time():
                # Claude is known to be at its limit: don't spend an attempt finding out again.
                result: ExecResult = ExecResult(
                    ok=False, limit_reset_at=limited_until, executor=self.executor.name
                )
            else:
                result = await self.executor.run(req, on_event)
                if result.session_id:
                    self.store.update_task(t.id, session_id=result.session_id)
            if result.limit_reset_at and not result.ok and not result.stopped:
                self._claude_limited_until = max(self._claude_limited_until, result.limit_reset_at)
                fallback = (
                    await self._fallback_after_limit(t, project, req, tb, report, on_event)
                    if tb is not None
                    else None  # computer tasks wait for Claude (ADR 0008)
                )
                if fallback is None:
                    self._hold(t, report, result.limit_reset_at)
                    return
                result = fallback
            self._apply_result(report, result)
        except asyncio.CancelledError:
            report.status = "stopped"
            report.summary = report.summary or "Stopped on request."
        except Exception as e:
            log.exception("task %s crashed", t.id)
            report.status = "failed"
            report.errors.append(f"internal error: {e}")
        finally:
            await self._finish(t, report, tb, pc)

    # ------------------------------------------------------------------ usage limits

    async def _fallback_after_limit(
        self,
        t: TaskRow,
        project: ProjectConfig,
        req: ExecRequest,
        tb: gitops.TaskBranch,
        report: TaskReport,
        on_event: OnEvent,
    ) -> ExecResult | None:
        """Run on a fallback model if this task may, else return None (the task is held).

        Read tier: allowed when ``fallback.read_tier``. Work tier: only for projects with
        ``fallback_work: true``, on a ``fallback/task-<id>`` branch. Approve-tier actions
        never run on a fallback model (approvals are off there, so they are denied).
        """
        models = self.cfg.fallback.models
        if not models:
            return None
        if t.tier == "read":
            if not self.cfg.fallback.read_tier:
                return None
        elif not (project.fallback_work and tb.branch):
            return None
        else:
            new = f"fallback/task-{t.id}"
            await asyncio.to_thread(gitops.git, tb.project_dir, "branch", "-m", tb.branch, new)
            tb.branch = new
            report.branch = new
            self.store.update_task(t.id, branch=new)
            req.branch = new
        ex = self._fallback_factory(models[0])
        report.fallback_model = f"{models[0].name} ({models[0].model})"
        report.notes.append(
            "Claude's usage limit was reached, so a fallback model "
            f"({report.fallback_model}) did this instead. It is less capable than Claude, "
            "cannot search the web, and could not ask you for approvals."
        )
        self.store.audit("fallback_used", {"model": models[0].name}, task_id=t.id)
        req.resume_session_id = None
        return await ex.run(req, on_event)

    def _hold(self, t: TaskRow, report: TaskReport, reset_at: float) -> None:
        hold_until = reset_at + 60
        report.status = "held"
        report.held_until = hold_until
        when = time.strftime("%H:%M", time.localtime(hold_until))
        report.summary = (
            f"This task needs Claude, and Claude's usage limit is reached. "
            f"I'll run it automatically at {when}."
        )
        self.store.update_task(t.id, hold_until=hold_until, attempts=t.attempts + 1)
        self.store.audit("task_held", {"until": hold_until}, task_id=t.id)

    def _apply_result(self, report: TaskReport, result: ExecResult) -> None:
        report.executor = result.executor
        report.session_id = result.session_id
        report.usage = result.usage
        report.cost_usd = result.cost_usd
        s = result.structured or {}
        report.summary = (s.get("summary") or result.summary or "").strip()
        report.done = list(s.get("done") or [])
        report.not_done = list(s.get("not_done") or [])
        report.needs_from_you = list(s.get("needs_from_you") or [])
        report.errors += list(s.get("errors") or []) + list(result.errors)
        if result.stopped:
            report.status = "stopped"
        elif result.ok:
            report.status = "done"
        else:
            report.status = "failed"
        if result.limit_reset_at:
            when = time.strftime("%H:%M", time.localtime(result.limit_reset_at))
            report.notes.append(f"Claude usage limit reached; resets around {when}.")

    async def _finish(
        self,
        t: TaskRow,
        report: TaskReport,
        tb: gitops.TaskBranch | None,
        pc: computer.ComputerRun | None = None,
    ) -> None:
        self.store.expire_task_approvals(t.id)
        try:
            if pc is not None:
                report.files_changed = await asyncio.to_thread(pc.changes)
                await asyncio.to_thread(pc.cleanup)
                if pc.untracked:
                    report.notes.append(
                        "Too many files to track changes in: " + ", ".join(pc.untracked)
                    )
            if tb is not None:
                changes, problems = await asyncio.to_thread(
                    gitops.finalize, tb, f"tell-my-pc: task {t.id}\n\n{t.text[:500]}"
                )
                report.files_changed = [c.as_dict() for c in changes]
                report.errors += problems
                report.branch_kept = tb.kept
                if tb.branch is None and changes:
                    report.notes.append(
                        "Files differ from the last commit, but the task ran read-only: these "
                        "changes were already there."
                    )
            report.finished_at = time.time()
            if report.session_id and report.status in ("done", "failed", "stopped"):
                branch = report.branch if report.branch_kept else None
                self.store.set_session(
                    t.project,
                    report.session_id,
                    branch or _prev_branch(self.store, t.project),
                    t.id,
                )
            md = report.save(self.reports_dir)
            result = report.to_dict() | {"speak": report.speak()}
            self.store.update_task(
                t.id,
                status=report.status,
                result_json=json.dumps(result, default=str),
                report_path=str(md),
                error=report.errors[0] if report.errors and report.status != "done" else None,
            )
            self.store.audit(
                "task_finished",
                {"status": report.status, "files": len(report.files_changed)},
                task_id=t.id,
            )
            await self._notify(report.notification())
        except Exception:
            log.exception("finishing task %s failed", t.id)
            self.store.update_task(t.id, status="failed", error="could not finish the report")
        finally:
            self._running.pop(t.id, None)
            if self._running_projects.get(t.project) == t.id:
                self._running_projects.pop(t.project, None)
            self._kick.set()

    async def notify(self, note: Notification) -> None:
        """Send through the configured notifier; failures are logged, never raised."""
        await self._notify(note)

    async def _notify(self, note: Notification) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.send(note)
        except Exception as e:
            log.warning("notifier failed: %s", e)
            self.store.audit("notify_failed", {"error": str(e), "title": note.title})


def _prev_branch(store: Store, project: str) -> str | None:
    s = store.get_session(project)
    return s.get("branch") if s else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _say_id(task_id: str) -> str:
    return " ".join(task_id[:4])


def _task_public(t: TaskRow) -> dict[str, object]:
    return {
        "id": t.id,
        "project": t.project,
        "status": t.status,
        "tier": t.tier,
        "text": t.text[:200],
        "created_at": t.created_at,
        "branch": t.branch,
        "report_path": t.report_path,
    }


def _brief_denial(d: object) -> str:
    if isinstance(d, dict):
        tool = d.get("tool_name") or d.get("tool") or "?"
        detail = d.get("reason") or json.dumps(d.get("tool_input") or {}, default=str)[:120]
        return f"{tool}: {detail}"
    return str(d)[:200]
