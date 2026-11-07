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
from pathlib import Path

from . import gitops, paths
from .config import Config, ProjectConfig
from .plugins.api import (
    Action,
    ExecEvent,
    ExecRequest,
    ExecResult,
    Executor,
    IncomingTask,
    Notification,
    Notifier,
    SinkReply,
)
from .policy import build_spec
from .report import TaskReport
from .resolver import resolve
from .store import Store, TaskRow

log = logging.getLogger("tell_my_pc")

_CONTINUE = re.compile(r"^\s*(please\s+)?(continue|keep going|carry on|resume)\b", re.I)
MAX_TEXT = 4000


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

        res = resolve(self.cfg.projects, task.project, text)
        if res.project is None:
            options = ", ".join(p.name for p in res.candidates[:5])
            speak = f"Which project? I know {options}." if options else "No projects configured."
            await self._notify(
                Notification(
                    title="tell-my-pc: which project?",
                    message=f'For "{text[:200]}": {res.reason}. Say the project name, '
                    f"for example: {options}.",
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
            targets = [t] if t and t.status in ("queued", "running") else []
        else:
            for t in self.store.tasks(("queued", "running"), limit=500):
                if project is None or t.project.lower() == project.lower():
                    targets.append(t)
        if not targets:
            return SinkReply(True, "Nothing is running.")
        for t in targets:
            if t.status == "queued":
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
        while True:
            try:
                for row in self.store.unnotified_approvals():
                    await self._send_approval(row)
            except Exception:
                log.exception("approval loop error")
            await asyncio.sleep(0.2)

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
            await self._kick.wait()
            self._kick.clear()
            try:
                self._dispatch()
            except Exception:
                log.exception("scheduler error")

    def _dispatch(self) -> None:
        limit = self.cfg.limits.max_parallel_projects
        for t in self.store.tasks(("queued",), limit=500):
            if len(self._running) >= limit:
                break
            if t.project in self._running_projects:
                continue
            self.store.update_task(t.id, status="running")
            self._running_projects[t.project] = t.id
            self._running[t.id] = asyncio.create_task(self._run(t), name=f"task-{t.id}")

    def _project(self, name: str) -> ProjectConfig:
        for p in self.cfg.projects:
            if p.name == name:
                return p
        raise KeyError(name)

    # ------------------------------------------------------------------ one task

    async def _run(self, t: TaskRow) -> None:
        report = TaskReport(id=t.id, project=t.project, asked=t.text, tier=t.tier, status="failed")
        tb: gitops.TaskBranch | None = None
        try:
            project = self._project(t.project)
            pdir = str(Path(project.path).expanduser().resolve())
            state_dir = self.tasks_dir / t.id
            state_dir.mkdir(parents=True, exist_ok=True)

            session = self.store.get_session(t.project) if t.resume else None
            if t.resume and not session:
                report.notes.append("No earlier session for this project, so this started fresh.")
            start_from = session.get("branch") if session else None

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
            spec = build_spec(
                tier="read" if t.tier == "read" else "work",
                project_dir=pdir,
                task_id=t.id,
                work_commands=pol.work_commands,
                extra_read_roots=[str(Path(r).expanduser()) for r in pol.extra_read_roots],
                builtin_never=pol.builtin_never,
                extra_never_paths=pol.never_paths,
                extra_never_programs=pol.never_commands,
                protect_dirs=[str(paths.config_dir()), str(self.data_dir)],
                state_dir=str(state_dir),
                db_path=self.store.path if self.store.path != ":memory:" else None,
                approvals=self.approvals_enabled,
                approval_timeout_s=pol.approval_timeout_s,
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
                branch=tb.branch,
                timeout_s=self.cfg.limits.task_timeout_s,
            )
            report.resumed = bool(req.resume_session_id)

            async def on_event(ev: ExecEvent) -> None:
                if ev.kind == "denied":
                    d = ev.data.get("denial") or ev.data
                    report.denied.append(_brief_denial(d))
                elif ev.kind == "tool":
                    self.store.audit("tool_call", {"tool": ev.data.get("name")}, task_id=t.id)

            result: ExecResult = await self.executor.run(req, on_event)
            self._apply_result(report, result)
            if result.session_id:
                self.store.update_task(t.id, session_id=result.session_id)
        except asyncio.CancelledError:
            report.status = "stopped"
            report.summary = report.summary or "Stopped on request."
        except Exception as e:
            log.exception("task %s crashed", t.id)
            report.status = "failed"
            report.errors.append(f"internal error: {e}")
        finally:
            await self._finish(t, report, tb)

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

    async def _finish(self, t: TaskRow, report: TaskReport, tb: gitops.TaskBranch | None) -> None:
        self.store.expire_task_approvals(t.id)
        try:
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
