"""The local dashboard: watch and manage the daemon from the computer itself.

Served by the daemon process on ``127.0.0.1`` only (``dashboard.port``, default 8790) and
opened with ``tell-my-pc dashboard``. Every API call needs the local key from
``<data dir>/dashboard.key`` in an ``X-TMP-Key`` header. A web page in your browser cannot
add that header to a cross-site request without a CORS preflight, which this server never
grants, and the ``Host`` check stops DNS-rebinding tricks. The page itself holds no data.

What it shows comes from the state database: tasks, approvals, deliveries, and the audit log,
which records what came in (requests and the reply Siri spoke, approval answers, downloads)
and what went out (every notification), plus every tool call and its verdict.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__

if TYPE_CHECKING:
    from .config import Config
    from .daemon import Daemon
    from .store import Store

STATIC = Path(__file__).parent / "dashboard_static"
KEY_FILE = "dashboard.key"
HOST = "127.0.0.1"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def load_key(data_dir: Path) -> str:
    """The local dashboard key, created on first use (readable only by this user)."""
    path = data_dir / KEY_FILE
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    path.write_text(key, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return key


def url(cfg: Config, key: str) -> str:
    # The key goes in the fragment: browsers never send it to the server or in Referer.
    return f"http://{HOST}:{cfg.dashboard.port}/#key={key}"


# --------------------------------------------------------------------------- events

IN_KINDS = {"request", "approval_answer", "file_downloaded", "auth_failed", "inbox_rejected",
            "file_link_refused"}  # fmt: skip
OUT_KINDS = {"notification_sent", "notify_failed"}
TOOL_KINDS = {"tool_allowed", "tool_denied"}
# The daemon's own "tool_call" rows repeat what the hook records (with the verdict).
HIDDEN_KINDS = {"tool_call"}


def direction(kind: str, detail: dict[str, Any]) -> str:
    """in (from the phone), out (to the phone), tool (a tool call) or system."""
    if kind in IN_KINDS:
        return "in"
    if kind == "task_received" and detail.get("source") not in (None, "", "tailscale_http"):
        return "in"  # e.g. the ntfy inbox, which has no HTTP request of its own
    if kind in OUT_KINDS:
        return "out"
    if kind in TOOL_KINDS:
        return "tool"
    return "system"


def verdict(kind: str, detail: dict[str, Any]) -> str | None:
    """allow, asked (went to the phone, either answer) or deny, for the verdict mark."""
    if kind not in TOOL_KINDS:
        return None
    reason = str(detail.get("reason") or "")
    asked = reason.startswith(("approved on the phone", "denied on the phone", "needs approval",
                               "approval timed out"))  # fmt: skip
    if kind == "tool_allowed":
        return "asked" if asked else "allow"
    return "asked-deny" if asked else "deny"


def tool_name(raw: Any) -> str:
    """mcp__tell_my_pc__pc_open_app -> pc_open_app; the report tool reads as Report."""
    name = str(raw or "")
    if name == "StructuredOutput":
        return "Report"
    return re.sub(r"^mcp__tell_my_pc__", "", name)


def _brief_input(value: Any) -> str:
    if isinstance(value, dict):
        if "summary" in value and "done" in value:  # the structured report itself
            return str(value.get("summary") or "")[:160]
        for key in ("command", "file_path", "path", "pattern", "url", "query", "name", "action"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, default=str)[:160] if value else ""
    return str(value or "")[:160]


def describe(kind: str, d: dict[str, Any]) -> str:
    """One readable line for an audit row."""
    if kind == "request":
        path = str(d.get("path") or "")
        reply = str(d.get("reply") or "")
        return f"{path}  →  {reply}" if reply else path
    if kind == "task_received":
        return f'"{d.get("text") or ""}"  →  {d.get("project")}, {d.get("tier")}'
    if kind == "notification_sent":
        return str(d.get("title") or "")
    if kind == "notify_failed":
        return f"{d.get('title')}: {d.get('error')}"
    if kind in TOOL_KINDS:
        return f"{tool_name(d.get('tool'))}  {_brief_input(d.get('input'))}"
    if kind == "task_finished":
        n = d.get("files") or 0
        return f"{d.get('status')}, {n} file{'s' if n != 1 else ''} changed"
    if kind == "approval_answer":
        who = " on this computer" if d.get("by") == "dashboard" else ""
        ok = "" if d.get("accepted", True) else " (too late)"
        return f"{d.get('decision')}{who}{ok}"
    if kind == "approval_requested":
        return f"asked to approve {d.get('tool')}"
    if kind == "file_sent":
        return f"{d.get('file')} ({'attached' if d.get('attached') else 'link'})"
    if kind == "file_queued":
        return Path(str(d.get("path") or "")).name
    if kind == "auth_failed":
        return f"refused {d.get('client')}: {d.get('why')}"
    if kind in ("locked", "unlocked"):
        return str(d.get("reason") or d.get("by") or d.get("where") or "")
    if kind == "project_asked":
        return f'"{d.get("text") or ""}"'
    parts = [f"{k} {v}" for k, v in d.items() if isinstance(v, str | int | float) and v != ""]
    return ", ".join(parts)[:200]


def event(row: dict[str, Any]) -> dict[str, Any]:
    d = row.get("detail") or {}
    if not isinstance(d, dict):
        d = {"value": d}
    kind = str(row["kind"])
    return {
        "id": row["id"],
        "ts": row["ts"],
        "task": row.get("task_id"),
        "kind": kind,
        "dir": direction(kind, d),
        "verdict": verdict(kind, d),
        "tool": tool_name(d.get("tool")) if kind in TOOL_KINDS else None,
        "brief": _brief_input(d.get("input")) if kind in TOOL_KINDS else None,
        "text": describe(kind, d),
        "detail": d,
    }


# --------------------------------------------------------------------------- config view


def redacted_config(cfg: Config) -> dict[str, Any]:
    """The config with every secret value hidden (env var names are kept)."""

    def walk(v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            if set(v) <= {"value", "env"} and ("value" in v or "env" in v):
                return {"env": v.get("env"), "value": "hidden" if v.get("value") else None}
            return {k: walk(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, key) for x in v]
        if key == "topic" and isinstance(v, str) and len(v) > 8:
            return v[:4] + "…" + v[-2:]  # a secret topic is a password
        return v

    return walk(cfg.model_dump(mode="json"))


# --------------------------------------------------------------------------- server


class Dashboard:
    def __init__(
        self, daemon: Daemon, store: Store, cfg: Config, data_dir: Path, port: int | None = None
    ) -> None:
        self.daemon = daemon
        self.store = store
        self.cfg = cfg
        self.data_dir = data_dir
        self.port = cfg.dashboard.port if port is None else port
        self.key = load_key(data_dir).encode("utf-8")
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.app = self._build()

    # ------------------------------------------------------------------ app

    def _build(self) -> Starlette:
        api = [
            ("/api/overview", self.overview, ["GET"]),
            ("/api/events", self.events, ["GET"]),
            ("/api/tasks", self.tasks, ["GET"]),
            ("/api/tasks/{tid}", self.task, ["GET"]),
            ("/api/approvals", self.approvals, ["GET"]),
            ("/api/approvals/{aid}", self.answer, ["POST"]),
            ("/api/stop", self.stop, ["POST"]),
            ("/api/lock", self.lock, ["POST"]),
            ("/api/unlock", self.unlock, ["POST"]),
            ("/api/files", self.files, ["GET"]),
            ("/api/config", self.config, ["GET"]),
        ]
        routes = [Route(p, self._guard(h), methods=m) for p, h, m in api]
        routes += [
            Route("/health", self._health, methods=["GET"]),
            Route("/", self._static, methods=["GET"]),
            Route("/static/{path:path}", self._static, methods=["GET"]),
        ]
        return Starlette(routes=routes)

    def _host_ok(self, request: Request) -> bool:
        host = request.headers.get("host", "")
        return host in (f"{HOST}:{self.port}", f"localhost:{self.port}")

    def _guard(self, handler: Any) -> Any:
        async def guarded(request: Request) -> Response:
            if not self._host_ok(request):
                return _json({"error": "wrong host"}, 403)
            key = request.headers.get("x-tmp-key", "").encode("utf-8")
            if not hmac.compare_digest(key, self.key):
                return _json({"error": "missing or wrong dashboard key"}, 401)
            if request.method == "POST":
                ctype = request.headers.get("content-type", "")
                if not ctype.startswith("application/json"):
                    return _json({"error": "send JSON"}, 415)
            try:
                return await handler(request)
            except (KeyError, ValueError) as e:
                return _json({"error": str(e)}, 400)

        return guarded

    async def _health(self, request: Request) -> Response:
        if not self._host_ok(request):
            return _json({"error": "wrong host"}, 403)
        return _json({"ok": True, "app": "tell-my-pc-dashboard"})

    async def _static(self, request: Request) -> Response:
        if not self._host_ok(request):
            return _json({"error": "wrong host"}, 403)
        rel = request.path_params.get("path") or "index.html"
        path = (STATIC / rel).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return _json({"error": "not found"}, 404)
        ctype = _STATIC_TYPES.get(path.suffix)
        if not ctype:
            return _json({"error": "not found"}, 404)
        resp = FileResponse(path, media_type=ctype)
        for k, v in _SECURITY_HEADERS.items():
            resp.headers[k] = v
        if path.suffix == ".woff2":
            resp.headers["Cache-Control"] = "max-age=604800"
        return resp

    # ------------------------------------------------------------------ reads

    async def overview(self, request: Request) -> Response:
        now = time.time()
        day = self.store.audit_since(since_ts=now - 86400, limit=20000)
        counts: dict[str, int] = {}
        for r in day:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        tool_rows = [event(r) for r in day if r["kind"] in TOOL_KINDS]
        verdicts = {"allow": 0, "asked": 0, "deny": 0}
        for e in tool_rows:
            v = str(e["verdict"])
            verdicts["asked" if v.startswith("asked") else v] += 1
        tasks = self.store.tasks(limit=500)
        recent = [t for t in tasks if t.created_at >= now - 86400]
        wire = [
            {"ts": r["ts"], "dir": e["dir"], "kind": r["kind"], "task": r.get("task_id"),
             "text": e["text"], "notekind": (r["detail"] or {}).get("kind")}
            for r in day
            if (e := event(r))["dir"] in ("in", "out")
        ]  # fmt: skip
        pending = [a for a in self.store.approvals(50) if a["status"] == "pending"
                   and a["expires_at"] > now]  # fmt: skip
        c = self.cfg
        return _json(
            {
                "version": __version__,
                "now": now,
                "started_at": self.daemon.started_at,
                "locked": self.store.locked(),
                "claude_limited_until": self.daemon.claude_limited_until or None,
                "executor": c.executor.type,
                "notifier": getattr(self.daemon.notifier, "name", None),
                "listening": c.server.public_url or f"{c.server.bind}:{c.server.port}",
                "computer": c.computer.enabled,
                "targets": [p.name for p in self.daemon.targets],
                "running": self.daemon.running(),
                "queued": len([t for t in tasks if t.status == "queued"]),
                "held": len([t for t in tasks if t.status == "held"]),
                "pending_approvals": pending,
                "day": {
                    "tasks": len(recent),
                    "done": len([t for t in recent if t.status == "done"]),
                    "failed": len([t for t in recent if t.status in ("failed", "refused")]),
                    "received": counts.get("request", 0),
                    "sent": counts.get("notification_sent", 0),
                    "send_failed": counts.get("notify_failed", 0),
                    "auth_failed": counts.get("auth_failed", 0),
                    "tools": verdicts,
                },
                "wire": wire,
                # when the computer was working: the line thickens there
                "spans": [
                    {
                        "id": t.id,
                        "status": t.status,
                        "start": t.created_at,
                        "end": now if t.status in ("running", "queued") else t.updated_at,
                    }
                    for t in tasks
                    if t.status != "held" and (t.updated_at >= now - 86400 or t.status == "running")
                ],
            }
        )

    async def events(self, request: Request) -> Response:
        q = request.query_params
        before = int(q.get("before") or 0)
        limit = max(1, min(1000, int(q.get("limit") or 200)))
        if before:
            rows = list(reversed(self.store.audit_before(before, limit)))
        else:
            rows = self.store.audit_since(
                after_id=int(q.get("after") or 0), limit=limit, task_id=q.get("task") or None
            )
        return _json({"events": [event(r) for r in rows if r["kind"] not in HIDDEN_KINDS]})

    async def tasks(self, request: Request) -> Response:
        limit = max(1, min(500, int(request.query_params.get("limit") or 100)))
        out = []
        for t in self.store.tasks(limit=limit):
            res = t.result or {}
            out.append(
                {
                    "id": t.id, "project": t.project, "status": t.status, "tier": t.tier,
                    "text": t.text, "created_at": t.created_at, "updated_at": t.updated_at,
                    "source": t.source, "summary": res.get("summary") or t.error or "",
                    "files": len(res.get("files_changed") or []),
                    "target": res.get("target", "project"),
                }
            )  # fmt: skip
        return _json({"tasks": out})

    async def task(self, request: Request) -> Response:
        tid = str(request.path_params["tid"])
        if not re.fullmatch(r"[0-9a-f]{8}", tid):
            raise ValueError("bad task id")
        t = self.store.get_task(tid)
        if t is None:
            return _json({"error": "no such task"}, 404)
        report = ""
        if t.report_path and Path(t.report_path).is_file():
            report = Path(t.report_path).read_text(encoding="utf-8")[:200_000]
        rows = self.store.audit_since(task_id=tid, limit=2000)
        return _json(
            {
                "task": {
                    "id": t.id,
                    "project": t.project,
                    "status": t.status,
                    "tier": t.tier,
                    "text": t.text,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "source": t.source,
                    "branch": t.branch,
                    "error": t.error,
                },
                "result": t.result,
                "report": report,
                "events": [event(r) for r in rows if r["kind"] not in HIDDEN_KINDS],
            }
        )

    async def approvals(self, request: Request) -> Response:
        return _json({"approvals": self.store.approvals(100), "now": time.time()})

    async def files(self, request: Request) -> Response:
        rows = self.store.deliveries(100)
        return _json(
            {
                "files": [
                    {
                        "id": r["id"],
                        "task": r["task_id"],
                        "name": Path(r["path"]).name,
                        "path": r["path"],
                        "size": r["size"],
                        "note": r["note"],
                        "created_at": r["created_at"],
                        "expires_at": r["expires_at"],
                        "sent": bool(r["notified"]),
                    }
                    for r in rows
                ],
                "now": time.time(),
            }
        )

    async def config(self, request: Request) -> Response:
        c = self.cfg.computer
        return _json(
            {
                "config": redacted_config(self.cfg),
                "computer": {
                    "enabled": c.enabled,
                    "workspace": c.workspace,
                    "write_roots": c.write_roots,
                    "read_roots": c.read_roots,
                    "blocked": c.blocked,
                },
            }
        )

    # ------------------------------------------------------------------ actions

    async def _json_body(self, request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 10_000:
            raise ValueError("body too large")
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("send a JSON object")
        return data

    async def answer(self, request: Request) -> Response:
        aid = str(request.path_params["aid"])
        if not re.fullmatch(r"[0-9a-f]{12}", aid):
            raise ValueError("bad approval id")
        body = await self._json_body(request)
        if body.get("decision") not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny")
        ok = self.daemon.answer_locally(aid, body["decision"] == "approve")
        if not ok:
            return _json({"ok": False, "error": "That approval expired or was answered."}, 409)
        return _json({"ok": True})

    async def stop(self, request: Request) -> Response:
        body = await self._json_body(request)
        tid = str(body.get("id") or "") or None
        r = await self.daemon.stop(tid, None)
        return _json({"ok": r.ok, "speak": r.speak})

    async def lock(self, request: Request) -> Response:
        body = await self._json_body(request)
        r = await self.daemon.lock(str(body.get("reason") or "locked from the dashboard"))
        return _json({"ok": r.ok, "speak": r.speak})

    async def unlock(self, request: Request) -> Response:
        await self._json_body(request)
        self.daemon.unlock("dashboard")
        return _json({"ok": True})

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app, host=HOST, port=self.port, log_level="warning", log_config=None,
            lifespan="off", access_log=False, server_header=False,
        )  # fmt: skip
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(), name="tell-my-pc-dashboard")
        for _ in range(200):
            if self._server.started:
                break
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.05)
        if self._server.servers and self._server.servers[0].sockets:
            self.port = self._server.servers[0].sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=10)


def _json(data: Any, status: int = 200) -> JSONResponse:
    resp = JSONResponse(data, status_code=status)
    for k, v in _SECURITY_HEADERS.items():
        resp.headers[k] = v
    return resp
