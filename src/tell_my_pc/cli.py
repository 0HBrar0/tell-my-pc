"""Command line: ``tell-my-pc serve | check | init | send | status | paths``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from . import __version__, paths
from .config import (
    Config,
    ConfigError,
    NtfyConfig,
    PluginConfig,
    load,
    resolve_bind,
    safety_problems,
    safety_warnings,
)
from .plugins.api import Executor, Notifier, Transport


def build_executor(cfg: Config) -> Executor:
    if cfg.executor.type == "fake":
        from .plugins.executors.fake import FakeExecutor

        return FakeExecutor()
    if cfg.executor.type == "claude_code":
        from .plugins.executors.claude_code import ClaudeCodeExecutor

        return ClaudeCodeExecutor(cfg.executor)
    from .plugins import registry

    return registry.load("executor", cfg.executor.type, cfg.executor.options, paths.data_dir())


def _one_notifier(n: NtfyConfig | PluginConfig) -> Notifier:
    if isinstance(n, NtfyConfig):
        from .plugins.notifiers.ntfy import NtfyNotifier

        return NtfyNotifier(n.server, n.topic, n.token.resolve() if n.token else None)
    from .plugins import registry

    return registry.load("notifier", n.type, n.options, paths.data_dir())


def build_notifier(cfg: Config) -> Notifier | None:
    main = _one_notifier(cfg.notifier) if cfg.notifier is not None else None
    if not cfg.also_notify:
        return main
    from .plugins.notifiers.fanout import FanoutNotifier

    return FanoutNotifier(main, [_one_notifier(n) for n in cfg.also_notify])


def build_extra_transports(cfg: Config) -> list[Transport]:
    from .plugins import registry

    return [registry.load("transport", t.type, t.options, paths.data_dir()) for t in cfg.transports]


def _load_checked(path: Path | None) -> Config:
    cfg = load(path)
    problems = safety_problems(cfg)
    if problems:
        raise ConfigError("Refusing to run with an unsafe config:\n- " + "\n- ".join(problems))
    return cfg


# --------------------------------------------------------------------------- commands


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    problems = safety_problems(cfg)
    for w in safety_warnings(cfg):
        print(f"warning: {w}")
    if problems:
        print("Unsafe config:", file=sys.stderr)
        for p in problems:
            print(f"- {p}", file=sys.stderr)
        return 1
    try:
        print(f"would listen on {resolve_bind(cfg)}:{cfg.server.port}")
    except ConfigError as e:
        print(f"bind: {e}", file=sys.stderr)
        return 1
    if cfg.executor.type == "claude_code":
        from .plugins.executors.claude_code import find_claude, find_git_bash

        try:
            print(f"claude: {find_claude(cfg.executor.claude_path)}")
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 1
        if sys.platform == "win32":
            bash = find_git_bash(cfg.executor.git_bash_path)
            print(f"git bash: {bash or 'not found (Work tier shell commands will be unavailable)'}")
    from .plugins.registry import PluginError

    try:
        build_notifier(cfg)
        build_extra_transports(cfg)
        if cfg.executor.type not in ("claude_code", "fake"):
            build_executor(cfg)
    except PluginError as e:
        print(f"plugin: {e}", file=sys.stderr)
        return 1
    print("config OK")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    log_kwargs: dict[str, object] = {}
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        # Under pythonw (autostart) there is no console: sys.stdout/stderr are None and
        # anything that writes to them crashes the daemon. Send them to the log file too.
        stream = open(args.log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
        log_kwargs["stream"] = stream
    elif sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        **log_kwargs,  # type: ignore[arg-type]
    )
    try:
        cfg = _load_checked(args.config)
        host = resolve_bind(cfg)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(cfg, host))
    return 0


async def _serve(cfg: Config, host: str) -> None:
    from .daemon import Daemon
    from .delivery import load_key
    from .plugins.transports.tailscale_http import TailscaleHttpTransport
    from .store import Store

    data = paths.data_dir()
    store = Store(data / "state.db")
    daemon = Daemon(cfg, store, build_executor(cfg), build_notifier(cfg), data)
    token = cfg.server.token.resolve()
    assert token
    transport = TailscaleHttpTransport(
        host,
        cfg.server.port,
        token,
        store,
        cfg.server.max_clock_skew_s,
        cfg.server.rate_limit_per_minute,
        file_key=load_key(data),
    )
    extras = build_extra_transports(cfg)
    if cfg.inbox is not None:
        from .plugins.transports.ntfy_inbox import NtfyInboxTransport

        ib = cfg.inbox
        extras.append(
            NtfyInboxTransport(
                ib.server,
                ib.topic,
                ib.token.resolve() or "",
                ib.secret.resolve() or "",
                store,
                reply=daemon.notify,
                max_clock_skew_s=cfg.server.max_clock_skew_s,
            )
        )
    await daemon.start()
    await transport.start(daemon)
    for t in extras:
        await t.start(daemon)
    logging.getLogger("tell_my_pc").info(
        "tell-my-pc %s listening on http://%s:%s", __version__, host, transport.port
    )
    try:
        await asyncio.Event().wait()
    finally:
        for t in extras:
            await t.close()
        await transport.close()
        await daemon.close()
        store.close()


def cmd_init(args: argparse.Namespace) -> int:
    target = args.config or paths.config_file()
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite).", file=sys.stderr)
        return 1
    project = Path(args.project).expanduser().resolve()
    token = secrets.token_urlsafe(32)
    cfg = {
        "version": 1,
        "server": {"bind": args.bind, "port": 8787, "token": {"value": token}},
        "projects": [
            {"name": args.name or project.name, "path": str(project), "default_tier": "work"}
        ],
        "notifier": {
            "type": "ntfy",
            "server": "https://ntfy.sh",
            "topic": f"tmp-{secrets.token_urlsafe(18)}",
            "token": {"env": "NTFY_TOKEN"},
        },
    }
    Config.model_validate(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# tell-my-pc config (private: never commit this file)\n"
        + yaml.safe_dump(cfg, sort_keys=False),
        encoding="utf-8",
    )
    print(f"wrote {target}")
    print(f"ntfy topic: {cfg['notifier']['topic']}")
    print("bearer token (put it in the iOS Shortcut, then clear your terminal):")
    print(token)
    return 0


def _client_url(cfg: Config, url: str | None) -> str:
    if url:
        return url.rstrip("/")
    if cfg.server.public_url:
        return cfg.server.public_url.rstrip("/")
    return f"http://{resolve_bind(cfg)}:{cfg.server.port}"


def _post(cfg: Config, url: str, path: str, body: dict[str, object]) -> dict[str, object]:
    token = cfg.server.token.resolve()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TMP-Timestamp": str(int(time.time())),
        "X-TMP-Nonce": secrets.token_hex(12),
    }
    r = httpx.post(url + path, json=body, headers=headers, timeout=30)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "speak": f"HTTP {r.status_code}"}


def cmd_send(args: argparse.Namespace) -> int:
    """Act like the iOS Shortcut: POST a task and print what Siri would say."""
    cfg = load(args.config)
    body: dict[str, object] = {"text": " ".join(args.text)}
    if args.project:
        body["project"] = args.project
    if args.tier:
        body["tier"] = args.tier
    out = _post(cfg, _client_url(cfg, args.url), "/continue" if args.resume else "/task", body)
    print(json.dumps(out, indent=2) if args.json else out.get("speak"))
    return 0 if out.get("ok") else 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    out = _post(cfg, _client_url(cfg, args.url), "/status", {"id": args.id} if args.id else {})
    print(json.dumps(out, indent=2) if args.json else out.get("speak"))
    return 0 if out.get("ok") else 1


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    body = {k: v for k, v in (("id", args.id), ("project", args.project)) if v}
    out = _post(cfg, _client_url(cfg, args.url), "/stop", body)
    print(out.get("speak"))
    return 0 if out.get("ok") else 1


def _store():  # type: ignore[no-untyped-def]
    from .store import Store

    return Store(paths.data_dir() / "state.db")


def cmd_lock(args: argparse.Namespace) -> int:
    """Lock locally (works even if the daemon is down), then tell the daemon to stop tasks."""
    s = _store()
    s.set_locked(True, args.reason or "locked on the computer")
    s.audit("locked", {"reason": args.reason, "where": "cli"})
    s.close()
    print("Locked. Every new task and every tool call is refused until `tell-my-pc unlock`.")
    with contextlib.suppress(Exception):
        cfg = load(args.config)
        _post(cfg, _client_url(cfg, None), "/lock", {"reason": args.reason or ""})
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    """Only possible on the computer itself: the phone can lock but never unlock."""
    s = _store()
    was = s.locked()
    s.set_locked(False)
    s.audit("unlocked", {"where": "cli"})
    s.close()
    print("Unlocked." if was else "Was not locked.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    s = _store()
    rows = s.audit_log(args.task, limit=args.limit)
    s.close()
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for r in reversed(rows):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        detail = json.dumps(r["detail"], default=str)
        if len(detail) > 140:
            detail = detail[:140] + "…"
        print(f"{ts}  {r['task_id'] or '-':8}  {r['kind']:18}  {detail}")
    return 0


TASK_NAME = "tell-my-pc"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def autostart_script(action: str, config: Path | None = None) -> str:
    """PowerShell for Windows autostart at user logon (Task Scheduler, not a Windows Service).

    ``schtasks /SC ONLOGON`` needs admin rights; Register-ScheduledTask with a logon trigger
    for the current user does not. The task gets explicit --config and --data-dir, so it does
    not depend on environment variables the Task Scheduler may not have picked up yet.
    """
    name = _ps_quote(TASK_NAME)
    if action == "remove":
        return f"Unregister-ScheduledTask -TaskName {name} -Confirm:$false; 'removed'"
    if action == "status":
        return (
            f"$t = Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; "
            "if ($t) { $i = $t | Get-ScheduledTaskInfo; "
            '"state: $($t.State)  last run: $($i.LastRunTime)  result: $($i.LastTaskResult)" }'
            " else { 'not installed' }"
        )
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    cfg = (config or paths.config_file()).resolve()
    data = paths.data_dir().resolve()
    argline = (
        f'-m tell_my_pc.cli --config "{cfg}" --data-dir "{data}" '
        f'serve --log-file "{data / "daemon.log"}"'
    )
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            '$user = "$env:USERDOMAIN\\$env:USERNAME"',
            f"$a = New-ScheduledTaskAction -Execute {_ps_quote(str(runner))} "
            f"-Argument {_ps_quote(argline)} -WorkingDirectory {_ps_quote(str(data))}",
            "$t = New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$p = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive "
            "-RunLevel Limited",
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)",
            f"Register-ScheduledTask -TaskName {name} -Action $a -Trigger $t -Principal $p "
            "-Settings $s -Force | Out-Null",
            f"Start-ScheduledTask -TaskName {name}",
            "'installed and started'",
        ]
    )


def cmd_autostart(args: argparse.Namespace) -> int:
    if sys.platform != "win32" and not args.dry_run:
        print("Autostart is built in for Windows. On macOS/Linux use launchd or systemd --user;"
              " see docs/setup/autostart.md.", file=sys.stderr)  # fmt: skip
        return 1
    script = autostart_script(args.action, args.config)
    if args.dry_run:
        print(script)
        return 0
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    print((r.stdout or r.stderr).strip())
    return r.returncode


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_wizard import run

    return run(args)


def cmd_plugins(args: argparse.Namespace) -> int:
    from .plugins import registry
    from .plugins.api import PLUGIN_API_VERSION

    print(f"plugin API {PLUGIN_API_VERSION}")
    for info in registry.list_plugins():
        src = "built in" if info.builtin else f"{info.distribution or '?'} ({info.target})"
        print(f"{info.kind:10} {info.name:20} {src}")
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    print(f"config: {paths.config_file()}")
    print(f"data:   {paths.data_dir()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tell-my-pc", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", type=Path, default=None, help="config file (default: user dir)")
    ap.add_argument(
        "--data-dir", type=Path, default=None, help="state, reports and logs (default: user dir)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the daemon")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", type=Path, help="log here instead of the console")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("check", help="validate config and safety rules").set_defaults(func=cmd_check)

    p = sub.add_parser("lock", help="kill switch: refuse all tasks and tool calls")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_lock)
    sub.add_parser("unlock", help="undo lock (only on this computer)").set_defaults(func=cmd_unlock)

    p = sub.add_parser("audit", help="show the audit log")
    p.add_argument("--task")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("autostart", help="start the daemon at logon (Windows Task Scheduler)")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.add_argument("--dry-run", action="store_true", help="print the command only")
    p.set_defaults(func=cmd_autostart)
    sub.add_parser("paths", help="print config and data locations").set_defaults(func=cmd_paths)
    sub.add_parser("plugins", help="list installed plugins").set_defaults(func=cmd_plugins)

    p = sub.add_parser("setup", help="guided first-time setup (recommended)")
    p.add_argument("--projects-dir", type=Path, help="folder that holds your git projects")
    p.add_argument("--bind", help="override the Tailscale address (e.g. 127.0.0.1 for tests)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--yes", action="store_true", help="accept every default, no questions")
    p.add_argument("--force", action="store_true", help="replace an existing config")
    p.add_argument("--no-open", action="store_true", help="don't open the phone page")
    p.add_argument("--no-autostart", action="store_true")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("init", help="write a minimal starter config (see also: setup)")
    p.add_argument("--project", required=True, help="folder of your first project")
    p.add_argument("--name", help="spoken name of that project")
    p.add_argument("--bind", default="tailscale")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    for name, func, help_ in (
        ("send", cmd_send, "send a task like the iOS Shortcut does"),
        ("status", cmd_status, "ask the daemon for status"),
        ("stop", cmd_stop, "stop running tasks"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--url", help="daemon URL (default: from config)")
        p.add_argument("--json", action="store_true")
        if name == "send":
            p.add_argument("text", nargs="+")
            p.add_argument("--project")
            p.add_argument("--tier", choices=["read", "work"])
            p.add_argument("--resume", action="store_true", help="continue the last session")
        else:
            p.add_argument("--id")
            if name == "stop":
                p.add_argument("--project")
        p.set_defaults(func=func)

    args = ap.parse_args(argv)
    # Windows consoles/pipes may use a legacy code page: never crash on ✓ or names.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(errors="replace")
    # Explicit locations win over the platform defaults, for this process and its children
    # (the policy hook protects the config and data dirs by these paths).
    if args.data_dir:
        os.environ["TELL_MY_PC_DATA_DIR"] = str(args.data_dir.expanduser().resolve())
    if args.config:
        os.environ["TELL_MY_PC_CONFIG_DIR"] = str(args.config.expanduser().resolve().parent)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
