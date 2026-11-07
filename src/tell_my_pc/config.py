"""Config model, loading and safety checks.

Real config lives in the user's config dir (see :mod:`tell_my_pc.paths`). The repo only ships
``config/example.yaml`` and ``config/schema.json`` (generated from these models).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import paths
from .rules import NEVER_PROGRAMS, program_name

MIN_TOKEN_LEN = 32
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Secret(_Model):
    """A secret is either inline (only in the user's config dir) or read from an env var."""

    value: str | None = None
    env: str | None = None

    def resolve(self) -> str | None:
        if self.env:
            return os.environ.get(self.env) or None
        return self.value or None


class ServerConfig(_Model):
    bind: str = Field(
        "tailscale",
        description="'tailscale' (auto-detect this machine's Tailscale IP), a Tailscale IP, "
        "or 127.0.0.1 for local testing. All-interface binds are refused.",
    )
    port: int = Field(8787, ge=0, le=65535, description="0 picks a free port (tests).")
    token: Secret = Field(default_factory=lambda: Secret(env="TELL_MY_PC_TOKEN"))
    max_clock_skew_s: int = Field(300, ge=10, le=3600)
    rate_limit_per_minute: int = Field(20, ge=1, le=600)
    public_url: str | None = Field(
        None,
        description="How the phone reaches the daemon, e.g. https://mypc.tailnet.ts.net:8787. "
        "Used for approval callbacks and links.",
    )


class LimitsConfig(_Model):
    max_parallel_projects: int = Field(2, ge=1, le=16)
    task_timeout_s: int = Field(3600, ge=30)


class ProjectConfig(_Model):
    name: str
    path: str
    aliases: list[str] = Field(default_factory=list)
    default_tier: Literal["read", "work"] = "work"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("project name must not be empty")
        return v.strip()


DEFAULT_WORK_COMMANDS: list[str] = [
    "pytest",
    "python -m pytest",
    "uv run pytest",
    "uv run ruff check",
    "uv run pyright",
    "npm test",
    "npm run test",
    "npm run build",
    "npm run lint",
    "cargo test",
    "cargo build",
    "go test",
    "go build",
    "dotnet test",
    "dotnet build",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git add",
    "git commit",
]


class PolicyConfig(_Model):
    work_commands: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WORK_COMMANDS),
        description="Command prefixes the Work tier may run without approval.",
    )
    extra_read_roots: list[str] = Field(
        default_factory=list, description="Folders outside the project the Read tier may read."
    )
    builtin_never: bool = Field(
        True, description="Apply the built-in Never rules (system folders, credential files)."
    )
    never_paths: list[str] = Field(default_factory=list, description="Extra Never path globs.")
    never_commands: list[str] = Field(default_factory=list, description="Extra Never programs.")
    approval_timeout_s: int = Field(300, ge=10, le=86400)


class NtfyConfig(_Model):
    type: Literal["ntfy"] = "ntfy"
    server: str = "https://ntfy.sh"
    topic: str
    token: Secret | None = Field(default_factory=lambda: Secret(env="NTFY_TOKEN"))


class ExecutorConfig(_Model):
    type: Literal["claude_code", "fake"] = "claude_code"
    claude_path: str | None = Field(None, description="Path to claude; default: found on PATH.")
    model: str | None = None
    max_turns: int | None = Field(None, ge=1)
    setting_sources: list[Literal["user", "project", "local"]] = Field(
        default_factory=lambda: ["user"],
        description="Claude settings sources to load. Project/local are off by default so a "
        "repo cannot widen permissions or add hooks.",
    )


class Config(_Model):
    version: Literal[1] = 1
    server: ServerConfig = Field(default_factory=lambda: ServerConfig.model_validate({}))
    limits: LimitsConfig = Field(default_factory=lambda: LimitsConfig.model_validate({}))
    projects: list[ProjectConfig]
    policy: PolicyConfig = Field(default_factory=lambda: PolicyConfig.model_validate({}))
    notifier: NtfyConfig | None = None
    executor: ExecutorConfig = Field(default_factory=lambda: ExecutorConfig.model_validate({}))


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        raise ConfigError(f"No config at {path}. Run `tell-my-pc init` first.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid config {path}:\n{e}") from e


# --------------------------------------------------------------------------- safety checks


def tailscale_ip() -> str | None:
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.split():
        try:
            if ipaddress.ip_address(line) in TAILSCALE_V4:
                return line
        except ValueError:
            continue
    return None


def resolve_bind(cfg: Config) -> str:
    """Return the concrete IP to bind to, or raise if it is not allowed."""
    bind = cfg.server.bind.strip()
    if bind == "tailscale":
        ip = tailscale_ip()
        if not ip:
            raise ConfigError(
                "server.bind is 'tailscale' but no Tailscale IP was found. Is Tailscale running "
                "and logged in? (See docs/setup/tailscale.md.)"
            )
        return ip
    problems = check_bind(bind)
    if problems:
        raise ConfigError(problems[0])
    return bind


def check_bind(bind: str) -> list[str]:
    if bind == "tailscale":
        return []
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return [f"server.bind {bind!r} must be 'tailscale' or an IP address."]
    if ip.is_unspecified:
        return [f"server.bind {bind} listens on all interfaces. Refused."]
    if ip.is_loopback or ip in TAILSCALE_V4 or ip in TAILSCALE_V6:
        return []
    return [f"server.bind {bind} is not a Tailscale or loopback address. Refused."]


def safety_problems(cfg: Config) -> list[str]:
    """Everything that makes a config unsafe. An empty list means it may run."""
    problems: list[str] = []
    problems += check_bind(cfg.server.bind.strip())

    token = cfg.server.token.resolve()
    if not token:
        src = f"env var {cfg.server.token.env}" if cfg.server.token.env else "server.token"
        problems.append(f"No bearer token: set {src}.")
    elif len(token) < MIN_TOKEN_LEN:
        problems.append(f"Bearer token is shorter than {MIN_TOKEN_LEN} characters.")

    pol = cfg.policy
    if not pol.builtin_never and not pol.never_paths and not pol.never_commands:
        problems.append("The Never deny list is empty. Refused.")

    never = {program_name(p) for p in pol.never_commands} | NEVER_PROGRAMS
    for cmd in pol.work_commands:
        parts = cmd.split()
        if not parts:
            problems.append("policy.work_commands contains an empty entry.")
        elif program_name(parts[0]) in never:
            problems.append(f"policy.work_commands entry {cmd!r} runs a Never program.")

    if not cfg.projects:
        problems.append("No projects configured.")
    names: set[str] = set()
    for p in cfg.projects:
        key = p.name.lower()
        if key in names:
            problems.append(f"Duplicate project name {p.name!r}.")
        names.add(key)
        if not Path(p.path).expanduser().is_dir():
            problems.append(f"Project {p.name!r}: folder {p.path} does not exist.")

    if cfg.notifier is not None:
        n = cfg.notifier
        if not n.server.startswith("https://") and not _is_local_url(n.server):
            problems.append("notifier.server must use https:// (or be a local address).")
        if len(n.topic) < 12:
            problems.append(
                "notifier.topic is short enough to guess. Use at least 12 random characters."
            )
    return problems


def safety_warnings(cfg: Config) -> list[str]:
    warnings: list[str] = []
    if cfg.server.bind.strip() in ("127.0.0.1", "::1"):
        warnings.append("Bound to loopback: fine for testing, but your phone cannot reach it.")
    n = cfg.notifier
    if n is not None and (n.token is None or not n.token.resolve()):
        warnings.append(
            "ntfy has no access token: anyone who guesses the topic can read your reports. "
            "See docs/setup/ntfy.md."
        )
    if n is None:
        warnings.append("No notifier configured: reports are only saved to disk.")
    return warnings


def _is_local_url(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(".ts.net")
    return ip.is_loopback or ip in TAILSCALE_V4 or ip in TAILSCALE_V6
