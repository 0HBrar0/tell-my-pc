"""`tell-my-pc setup`: a guided first-time setup.

It finds what it can (Tailscale IP, git projects, the screenshots folder, the claude CLI),
generates the secrets, refuses unsafe configs, writes the private phone-setup page with QR
codes, and on Windows installs autostart. It only asks about what it can't decide.
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from . import gitops, paths, phone_page
from .config import Config, safety_problems, safety_warnings, tailscale_ip

FIREWALL_CMD = (
    'New-NetFirewallRule -DisplayName "tell-my-pc (Tailscale only)" -Direction Inbound '
    "-Protocol TCP -LocalPort {port} -RemoteAddress 100.64.0.0/10 -Action Allow "
    "-Profile Private,Domain"
)


class Prompter:
    """Asks on the terminal, or takes every default with ``--yes``."""

    def __init__(self, assume_yes: bool, read: Callable[[str], str] = input) -> None:
        self.assume_yes = assume_yes
        self.read = read

    def ask(self, question: str, default: str = "") -> str:
        if self.assume_yes:
            return default
        hint = f" [{default}]" if default else ""
        answer = self.read(f"{question}{hint}: ").strip()
        return answer or default

    def yes(self, question: str, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.read(f"{question} {hint}: ").strip().lower()
        return default if not answer else answer.startswith("y")


def find_repos(root: Path) -> list[Path]:
    """``root`` itself if it is a repo, else the git repos directly inside it."""
    root = root.expanduser().resolve()
    if gitops.is_repo(root):
        return [root]
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and gitops.is_repo(p)
    )


def screenshot_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    candidates = [
        home / "Pictures" / "Screenshots",
        home / "OneDrive" / "Pictures" / "Screenshots",
        home / "Desktop",  # macOS saves screenshots here
    ]
    if sys.platform != "darwin":
        candidates = candidates[:2]
    return [p for p in candidates if p.is_dir()]


def spoken_name(folder: str) -> str:
    return folder.replace("-", " ").replace("_", " ").strip()


def build_config(
    *,
    projects: list[Path],
    bind: str,
    public_url: str | None,
    port: int,
    screenshots: list[Path],
    ntfy_server: str,
    ntfy_token_env: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "server": {
            "bind": bind,
            "port": port,
            "token": {"value": secrets.token_urlsafe(32)},
            **({"public_url": public_url} if public_url else {}),
        },
        "projects": [
            {
                "name": spoken_name(p.name),
                "path": str(p),
                **({"aliases": [p.name]} if spoken_name(p.name) != p.name else {}),
                "default_tier": "work",
            }
            for p in projects
        ],
        "delivery": {
            "roots": [str(s) for s in screenshots],
            # Without an ntfy token, attachments would sit on a public server: links only.
            "attach_max_mb": 2 if ntfy_token_env else 0,
        },
        "notifier": {
            "type": "ntfy",
            "server": ntfy_server,
            "topic": f"tmp-{secrets.token_urlsafe(24)}",
            "token": {"env": ntfy_token_env} if ntfy_token_env else None,
        },
    }


def run(args: argparse.Namespace, read: Callable[[str], str] = input) -> int:
    say = print
    ask = Prompter(args.yes, read)
    target: Path = args.config or paths.config_file()
    say("tell-my-pc setup\n")

    replace_question = f"{target} already exists. Replace it?"
    if target.exists() and not args.force and not ask.yes(replace_question, default=False):
        say("Nothing changed.")
        return 1

    # 1. claude
    if shutil.which("claude"):
        say("✓ Claude Code CLI found")
    else:
        say("! Claude Code CLI not found. Install it and run `claude` once to log in.")

    # 2. Tailscale
    if args.bind:
        bind, ip = args.bind, args.bind
    else:
        ip = tailscale_ip()
        if not ip:
            say(
                "✗ No Tailscale address found. Install Tailscale, log in, then run setup again "
                "(docs/setup/tailscale.md)."
            )
            return 1
        bind = "tailscale"
        say(f"✓ Tailscale address {ip}")
    port = args.port
    # The IP, not the *.ts.net name: some phones can't resolve MagicDNS names.
    public_url = f"http://{ip}:{port}"

    # 3. projects
    root = Path(ask.ask("Folder that holds your projects", str(args.projects_dir or Path.cwd())))
    repos = find_repos(root)
    if not repos:
        say(
            f"✗ No git repositories in {root}. Run `git init` in a project, or pick another folder."
        )
        return 1
    say(f"Found {len(repos)} project(s):")
    for i, r in enumerate(repos, 1):
        say(f"  {i}. {spoken_name(r.name)}  ({r})")
    pick = ask.ask("Which ones? 'all' or numbers like 1,3", "all")
    if pick.strip().lower() != "all":
        try:
            idx = {int(x) for x in pick.replace(" ", "").split(",") if x}
            repos = [r for i, r in enumerate(repos, 1) if i in idx]
        except ValueError:
            say("✗ Could not read that list.")
            return 1
        if not repos:
            say("✗ No projects chosen.")
            return 1

    # 4. screenshots
    shots = [s for s in screenshot_dirs() if ask.yes(f"Allow sending files from {s}?", True)]

    # 5. ntfy
    token_env = ask.ask(
        "Env var holding an ntfy access token (blank: none, use a secret random topic)", ""
    )

    cfg_dict = build_config(
        projects=repos,
        bind=bind,
        public_url=public_url,
        port=port,
        screenshots=shots,
        ntfy_server="https://ntfy.sh",
        ntfy_token_env=token_env or None,
    )
    cfg = Config.model_validate(cfg_dict)
    problems = safety_problems(cfg)
    if problems:
        say("✗ Refusing to write an unsafe config:")
        for p in problems:
            say(f"  - {p}")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# tell-my-pc config (private: never commit this file)\n"
        + yaml.safe_dump(cfg_dict, sort_keys=False),
        encoding="utf-8",
    )
    say(f"✓ Config written to {target}")
    for w in safety_warnings(cfg):
        say(f"  note: {w}")

    page = phone_page.write(cfg, paths.data_dir())
    say(f"✓ Phone setup page (private, contains your token): {page}")
    if not args.no_open:
        webbrowser.open(page.as_uri())

    if sys.platform == "win32":
        say("\nWindows firewall: run this once in an administrator PowerShell:")
        say("  " + FIREWALL_CMD.format(port=port))
        if not args.no_autostart and ask.yes("Start tell-my-pc automatically when you log in?"):
            from .cli import autostart_script

            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 autostart_script("install", target)],
                capture_output=True,
                text=True,
                check=False,
            )  # fmt: skip
            say(("✓ " if r.returncode == 0 else "✗ ") + (r.stdout or r.stderr).strip())
    else:
        say("\nStart the daemon with `tell-my-pc serve` (see docs/setup/autostart.md).")

    say("\nNext: open the phone page on this computer and follow it with your iPhone.")
    return 0
