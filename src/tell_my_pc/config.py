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
from typing import Any, Literal

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
        default="tailscale",
        description="'tailscale' (auto-detect this machine's Tailscale IP), a Tailscale IP, "
        "or 127.0.0.1 for local testing. All-interface binds are refused.",
    )
    port: int = Field(default=8787, ge=0, le=65535, description="0 picks a free port (tests).")
    token: Secret = Field(default_factory=lambda: Secret(env="TELL_MY_PC_TOKEN"))
    max_clock_skew_s: int = Field(default=300, ge=10, le=3600)
    rate_limit_per_minute: int = Field(default=20, ge=1, le=600)
    public_url: str | None = Field(
        default=None,
        description="How the phone reaches the daemon, e.g. https://mypc.tailnet.ts.net:8787. "
        "Used for approval callbacks and links.",
    )


class FallbackModelConfig(_Model):
    """An OpenAI-compatible chat endpoint (OpenAI, Ollama, LM Studio, vLLM, ...)."""

    name: str
    base_url: str = Field(description="e.g. http://127.0.0.1:11434/v1 for Ollama.")
    model: str
    api_key: Secret | None = None
    max_steps: int = Field(default=12, ge=1, le=100)
    timeout_s: int = Field(default=600, ge=10)
    max_file_chars: int = Field(default=20_000, ge=1000, le=500_000)


class FallbackConfig(_Model):
    models: list[FallbackModelConfig] = Field(
        default_factory=list, description="Tried in order when Claude is at its usage limit."
    )
    read_tier: bool = Field(
        default=True, description="Run Read-tier tasks on a fallback model instead of waiting."
    )


class DeliveryConfig(_Model):
    """Files a task may send to the phone (Phase 4)."""

    roots: list[str] = Field(
        default_factory=list,
        description="Extra folders whose files tasks may send you, e.g. your Screenshots "
        "folder. The task's project folder is always allowed. Tasks may also read these.",
    )
    link_ttl_s: int = Field(default=3600, ge=60, le=7 * 86400)
    attach_max_mb: float = Field(
        default=2.0, ge=0, le=50, description="Send files up to this size as ntfy attachments."
    )
    max_file_mb: float = Field(default=200.0, gt=0, le=4096)


class LimitsConfig(_Model):
    max_parallel_projects: int = Field(default=2, ge=1, le=16)
    task_timeout_s: int = Field(default=3600, ge=30)


class ProjectConfig(_Model):
    name: str
    path: str
    aliases: list[str] = Field(default_factory=list)
    default_tier: Literal["read", "work"] = "work"
    fallback_work: bool = Field(
        default=False,
        description="Let Work-tier tasks in this project run on a fallback model when Claude "
        "is at its usage limit. They run on a fallback/task-<id> branch.",
    )

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
        default=True,
        description="Apply the built-in Never rules (system folders, credential files).",
    )
    never_paths: list[str] = Field(default_factory=list, description="Extra Never path globs.")
    never_commands: list[str] = Field(default_factory=list, description="Extra Never programs.")
    approval_timeout_s: int = Field(default=300, ge=10, le=86400)


class NtfyConfig(_Model):
    type: Literal["ntfy"] = "ntfy"
    server: str = "https://ntfy.sh"
    topic: str
    token: Secret | None = Field(default_factory=lambda: Secret(env="NTFY_TOKEN"))


class NtfyInboxConfig(_Model):
    """Receive tasks through an ntfy topic, for when the phone has no Tailscale."""

    server: str = "https://ntfy.sh"
    topic: str = Field(description="A private topic only you can read and write.")
    token: Secret = Field(
        default_factory=lambda: Secret(env="NTFY_INBOX_TOKEN"),
        description="ntfy access token with read access to the topic. Required.",
    )
    secret: Secret = Field(
        default_factory=lambda: Secret(env="TELL_MY_PC_INBOX_SECRET"),
        description="Shared secret every inbox message must carry (32+ characters). It is "
        "not the HTTP bearer token: messages are stored on the ntfy server.",
    )


BUILTIN_NOTIFIERS = frozenset({"ntfy"})
BUILTIN_EXECUTORS = frozenset({"claude_code", "fake"})
BUILTIN_TRANSPORTS = frozenset({"tailscale_http", "ntfy_inbox"})


class PluginConfig(_Model):
    """A third-party plugin found through its Python entry point (see writing-a-plugin.md)."""

    type: str = Field(description="The plugin's entry-point name.")
    options: dict[str, Any] = Field(
        default_factory=dict, description="Passed to the plugin's factory as-is."
    )

    @field_validator("type")
    @classmethod
    def _not_builtin(cls, v: str) -> str:
        if v in BUILTIN_NOTIFIERS | BUILTIN_TRANSPORTS:
            raise ValueError(f"{v!r} is built in; configure it with its own section")
        if not v.strip():
            raise ValueError("plugin type must not be empty")
        return v


class ExecutorConfig(_Model):
    type: str = Field(
        default="claude_code",
        description="'claude_code', 'fake' (tests), or an executor plugin's entry-point name.",
    )
    options: dict[str, Any] = Field(
        default_factory=dict, description="Options for an executor plugin."
    )
    claude_path: str | None = Field(
        default=None, description="Path to claude; default: found on PATH."
    )
    git_bash_path: str | None = Field(
        default=None,
        description="Windows only: Git Bash for Claude Code hooks. Default: found next to git.",
    )
    model: str | None = None
    max_turns: int | None = Field(default=None, ge=1)
    setting_sources: list[Literal["user", "project", "local"]] = Field(
        default_factory=lambda: ["user"],
        description="Claude settings sources to load. Project/local are off by default so a "
        "repo cannot widen permissions or add hooks.",
    )


class ComputerConfig(_Model):
    """Tasks on the computer itself, outside any project (ADR 0008)."""

    enabled: bool = False
    name: str = Field(default="computer", description="Spoken name of this target.")
    aliases: list[str] = Field(
        default_factory=lambda: ["pc", "my pc", "my computer", "this computer", "laptop"]
    )
    workspace: str = Field(
        default="~/tell-my-pc",
        description="Folder these tasks fully control; scripts go in its tmp/ subfolder, which "
        "is emptied after every task.",
    )
    write_roots: list[str] = Field(
        default_factory=list,
        description="Other folders tasks may change (move, rename, create, Recycle Bin).",
    )
    read_roots: list[str] = Field(
        default_factory=list, description="Folders tasks may read but not change."
    )
    blocked: list[str] = Field(
        default_factory=list,
        description="Folders tasks may never read or touch, even with an approval.",
    )
    default_target: bool = Field(
        default=True, description="Tasks that name no project go to the computer."
    )
    actions: bool = Field(
        default=True,
        description="Curated actions: open apps/URLs, media keys, lock, screenshot, system info.",
    )

    @property
    def keys(self) -> list[str]:
        return [self.name, *self.aliases]


class Config(_Model):
    version: Literal[1] = 1
    computer: ComputerConfig = Field(default_factory=lambda: ComputerConfig.model_validate({}))
    server: ServerConfig = Field(default_factory=lambda: ServerConfig.model_validate({}))
    limits: LimitsConfig = Field(default_factory=lambda: LimitsConfig.model_validate({}))
    projects: list[ProjectConfig]
    policy: PolicyConfig = Field(default_factory=lambda: PolicyConfig.model_validate({}))
    notifier: NtfyConfig | PluginConfig | None = Field(
        default=None,
        union_mode="left_to_right",
        description="Main notifier: reports, questions and approval buttons.",
    )
    also_notify: list[NtfyConfig | PluginConfig] = Field(
        default_factory=list,
        description="Extra notifiers that get a copy of reports and questions (no approvals).",
    )
    fallback: FallbackConfig = Field(default_factory=lambda: FallbackConfig.model_validate({}))
    delivery: DeliveryConfig = Field(default_factory=lambda: DeliveryConfig.model_validate({}))
    inbox: NtfyInboxConfig | None = Field(
        default=None, description="Optional ntfy inbox transport (Phase 3)."
    )
    transports: list[PluginConfig] = Field(
        default_factory=list, description="Extra transport plugins, besides the Tailscale HTTP one."
    )
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

    if not cfg.projects and not cfg.computer.enabled:
        problems.append("No projects configured.")
    problems += computer_problems(cfg)
    names: set[str] = {cfg.computer.name.lower()} if cfg.computer.enabled else set()
    for p in cfg.projects:
        key = p.name.lower()
        if key in names:
            problems.append(f"Duplicate project name {p.name!r}.")
        names.add(key)
        if not Path(p.path).expanduser().is_dir():
            problems.append(f"Project {p.name!r}: folder {p.path} does not exist.")

    for m in cfg.fallback.models:
        if not (m.base_url.startswith("https://") or _is_local_url(m.base_url)):
            problems.append(f"fallback model {m.name!r}: base_url must be https:// or local.")
    problems += inbox_problems(cfg)
    ntfys = [n for n in [cfg.notifier, *cfg.also_notify] if isinstance(n, NtfyConfig)]
    for n in ntfys:
        if not n.server.startswith("https://") and not _is_local_url(n.server):
            problems.append("notifier.server must use https:// (or be a local address).")
        if len(n.topic) < 12:
            problems.append(
                "notifier.topic is short enough to guess. Use at least 12 random characters."
            )
    return problems


def _norm_dir(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(p))))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _system_dirs() -> list[str]:
    dirs = ["/", "/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr",
            "/var", "/System", "/Library", "/Applications"]  # fmt: skip
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        if os.environ.get(env):
            dirs.append(os.environ[env])
    return [_norm_dir(d) for d in dirs]


def computer_problems(cfg: Config) -> list[str]:
    """Folders a computer task may change must be specific, and never the system or our own."""
    c = cfg.computer
    if not c.enabled:
        return []
    out: list[str] = []
    home = _norm_dir("~")
    own = [_norm_dir(str(paths.config_dir())), _norm_dir(str(paths.data_dir()))]
    system = _system_dirs()
    blocked = [_norm_dir(b) for b in c.blocked]
    for label, raw in [("computer.workspace", c.workspace)] + [
        ("computer.write_roots", w) for w in c.write_roots
    ]:
        d = _norm_dir(raw)
        if os.path.dirname(d) == d:
            out.append(f"{label} {raw} is a whole drive. Pick a folder.")
        elif d == home:
            out.append(f"{label} {raw} is your whole home folder. Pick a folder inside it.")
        elif any(_inside(d, s) or _inside(s, d) for s in system if os.path.dirname(s) != s):
            out.append(f"{label} {raw} is or contains a system folder. Refused.")
        elif any(_inside(d, o) or _inside(o, d) for o in own):
            out.append(f"{label} {raw} overlaps tell-my-pc's own config or data folder.")
        elif any(_inside(d, b) or _inside(b, d) for b in blocked):
            out.append(f"{label} {raw} overlaps a computer.blocked folder.")
    if not c.name.strip():
        out.append("computer.name must not be empty.")
    return out


INBOX_MIN_TOPIC = 24


def inbox_problems(cfg: Config) -> list[str]:
    """Static checks for the ntfy inbox. Anyone who can publish to the inbox topic can give
    your computer tasks, so an open or guessable topic is refused. (At start-up the transport
    also checks that the topic refuses anonymous access.)"""
    ib = cfg.inbox
    if ib is None:
        return []
    out: list[str] = []
    if not ib.server.startswith("https://") and not _is_local_url(ib.server):
        out.append("inbox.server must use https:// (or be a local address).")
    if len(ib.topic) < INBOX_MIN_TOPIC:
        out.append(f"inbox.topic must be at least {INBOX_MIN_TOPIC} random characters.")
    if not ib.token.resolve():
        out.append("inbox has no ntfy access token: an unauthenticated topic cannot be an inbox.")
    secret = ib.secret.resolve()
    if not secret or len(secret) < MIN_TOKEN_LEN:
        out.append(f"inbox.secret must be set and at least {MIN_TOKEN_LEN} characters.")
    token = cfg.server.token.resolve()
    if secret and token and secret == token:
        out.append("inbox.secret must differ from the HTTP bearer token.")
    for n in [cfg.notifier, *cfg.also_notify]:
        if isinstance(n, NtfyConfig) and n.topic == ib.topic:
            out.append(
                "inbox.topic is also a notifier topic: the daemon would read its own reports "
                "as commands. Use a separate topic."
            )
    return out


def safety_warnings(cfg: Config) -> list[str]:
    warnings: list[str] = []
    if cfg.server.bind.strip() in ("127.0.0.1", "::1"):
        warnings.append("Bound to loopback: fine for testing, but your phone cannot reach it.")
    n = cfg.notifier
    if isinstance(n, NtfyConfig) and (n.token is None or not n.token.resolve()):
        warnings.append(
            "ntfy has no access token: anyone who guesses the topic can read your reports. "
            "See docs/setup/ntfy.md."
        )
    if n is None:
        warnings.append("No notifier configured: reports are only saved to disk.")
    for m in cfg.fallback.models:
        if not _is_local_url(m.base_url):
            warnings.append(
                f"Fallback model {m.name!r} is not on this machine or your tailnet: tasks that "
                "use it send project file contents to that service."
            )
    if cfg.executor.type not in BUILTIN_EXECUTORS:
        warnings.append(
            f"Executor plugin {cfg.executor.type!r} is trusted code: tell-my-pc's tiers only "
            "hold if it runs the policy hook on every tool call."
        )
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
