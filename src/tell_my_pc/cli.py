"""Command line: ``tell-my-pc serve | check | init | send | status | paths``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import secrets
import sys
import time
from pathlib import Path

import httpx
import yaml

from . import __version__, paths
from .config import Config, ConfigError, load, resolve_bind, safety_problems, safety_warnings
from .plugins.api import Executor, Notifier


def build_executor(cfg: Config) -> Executor:
    if cfg.executor.type == "fake":
        from .plugins.executors.fake import FakeExecutor

        return FakeExecutor()
    from .plugins.executors.claude_code import ClaudeCodeExecutor

    return ClaudeCodeExecutor(cfg.executor)


def build_notifier(cfg: Config) -> Notifier | None:
    n = cfg.notifier
    if n is None:
        return None
    from .plugins.notifiers.ntfy import NtfyNotifier

    return NtfyNotifier(n.server, n.topic, n.token.resolve() if n.token else None)


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
    print("config OK")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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
    )
    await daemon.start()
    await transport.start(daemon)
    logging.getLogger("tell_my_pc").info(
        "tell-my-pc %s listening on http://%s:%s", __version__, host, transport.port
    )
    try:
        await asyncio.Event().wait()
    finally:
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


def cmd_paths(args: argparse.Namespace) -> int:
    print(f"config: {paths.config_file()}")
    print(f"data:   {paths.data_dir()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tell-my-pc", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", type=Path, default=None, help="config file (default: user dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the daemon")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("check", help="validate config and safety rules").set_defaults(func=cmd_check)
    sub.add_parser("paths", help="print config and data locations").set_defaults(func=cmd_paths)

    p = sub.add_parser("init", help="write a starter config into the user config dir")
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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
