"""Permission tiers, enforced on every tool call by the PreToolUse hook.

Verdicts:

* ``allow`` — inside the task's tier.
* ``ask``   — outside the tier but not forbidden: needs a tap on the phone (Approve tier).
  Until approvals exist, and whenever an approval times out, ``ask`` becomes ``deny``.
* ``deny``  — a Never rule. No approval can unlock it.

This module must stay import-light: the hook imports it once per tool call.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .rules import NEVER_PATHS, NEVER_PROGRAMS, program_name

Verdict = Literal["allow", "ask", "deny"]
IS_WINDOWS = sys.platform == "win32"

READ_PATH_TOOLS: dict[str, str] = {
    "Read": "file_path",
    "NotebookRead": "notebook_path",
    "Grep": "path",
    "Glob": "path",
    "LS": "path",
}
EDIT_TOOLS: dict[str, str] = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}
WEB_TOOLS = frozenset({"WebFetch", "WebSearch"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
# Tools with no side effects outside the conversation. Subagent tool calls are hooked too.
SAFE_TOOLS = frozenset(
    {"TodoWrite", "Task", "Agent", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
     "TaskOutput", "BashOutput", "KillShell", "KillBash", "ExitPlanMode", "ToolSearch"}
)  # fmt: skip

READ_TIER_TOOLS = frozenset(READ_PATH_TOOLS) | WEB_TOOLS | SAFE_TOOLS
WORK_TIER_TOOLS = READ_TIER_TOOLS | frozenset(EDIT_TOOLS) | SHELL_TOOLS

# Folders inside a project that tasks may not write: git internals (hooks would run code
# outside the policy) and Claude project settings (could add hooks or permissions).
PROTECTED_PROJECT_DIRS = (".git", ".claude")

_GREP_SCAN_LIMIT = 20_000
_GREP_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist"})


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


@dataclass
class PolicySpec:
    """Everything the hook needs, written to a JSON file per task."""

    tier: Literal["read", "work"]
    project_dir: str
    task_id: str = ""
    read_roots: list[str] = field(default_factory=list)
    work_commands: list[list[str]] = field(default_factory=list)
    never_paths: list[str] = field(default_factory=lambda: list(NEVER_PATHS))
    never_programs: list[str] = field(default_factory=lambda: sorted(NEVER_PROGRAMS))
    state_dir: str = ""
    db_path: str | None = None
    home: str = field(default_factory=lambda: str(Path.home()))
    approval_url: str | None = None
    approval_timeout_s: float = 300

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> PolicySpec:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def taint_file(self) -> Path | None:
        return Path(self.state_dir) / "web_tainted" if self.state_dir else None

    def is_web_tainted(self) -> bool:
        tf = self.taint_file
        return bool(tf and tf.exists())

    def mark_web_tainted(self) -> None:
        tf = self.taint_file
        if tf:
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.touch()


def build_spec(
    *,
    tier: Literal["read", "work"],
    project_dir: str,
    task_id: str,
    work_commands: list[str],
    extra_read_roots: list[str],
    builtin_never: bool,
    extra_never_paths: list[str],
    extra_never_programs: list[str],
    protect_dirs: list[str],
    state_dir: str,
    db_path: str | None,
    approval_url: str | None = None,
    approval_timeout_s: float = 300,
) -> PolicySpec:
    never_paths = (list(NEVER_PATHS) if builtin_never else []) + list(extra_never_paths)
    never_paths += [d.rstrip("/\\") + "/**" for d in protect_dirs]
    never_programs = (sorted(NEVER_PROGRAMS) if builtin_never else []) + [
        program_name(p) for p in extra_never_programs
    ]
    return PolicySpec(
        tier=tier,
        project_dir=project_dir,
        task_id=task_id,
        read_roots=[project_dir, *extra_read_roots],
        work_commands=[c.split() for c in work_commands if c.split()],
        never_paths=never_paths,
        never_programs=never_programs,
        state_dir=state_dir,
        db_path=db_path,
        approval_url=approval_url,
        approval_timeout_s=approval_timeout_s,
    )


# --------------------------------------------------------------------------- paths

_MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")


def _fold(p: str) -> str:
    p = p.replace("\\", "/")
    if IS_WINDOWS:
        p = p.lower()
    if len(p) > 1 and p.endswith("/") and not p.endswith(":/"):
        p = p.rstrip("/")
    return p


def _expand(p: str, home: str) -> str:
    p = p.strip().strip('"').strip("'")
    if p == "~" or p.startswith(("~/", "~\\")):
        p = home + p[1:]
    if IS_WINDOWS:
        m = _MSYS_DRIVE.match(p.replace("\\", "/"))
        if m:  # git-bash style /c/Users -> C:/Users
            p = f"{m.group(1)}:/" + p.replace("\\", "/")[3:]
    return p


def norm_path(p: str, base: str, home: str) -> tuple[str, str]:
    """Return (lexical, resolved) folded forms of ``p`` relative to ``base``."""
    p = _expand(p, home)
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    lexical = _fold(os.path.normpath(p))
    resolved = _fold(os.path.realpath(p))
    return lexical, resolved


def within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _has_glob(s: str) -> bool:
    return any(ch in s for ch in "*?[")


def _never_match(path: str, patterns: list[str], home: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    for pat in patterns:
        if "/" not in pat.replace("\\", "/"):
            if fnmatch.fnmatchcase(name, _fold(pat)):
                return pat
            continue
        exp = _expand(pat, home)
        if exp.endswith(("/**", "\\**")) and not _has_glob(exp[:-3]):
            for root in {_fold(os.path.normpath(exp[:-3])), _fold(os.path.realpath(exp[:-3]))}:
                if within(path, root):
                    return pat
        elif fnmatch.fnmatchcase(path, _fold(exp)):
            return pat
    return None


class _PathCheck:
    def __init__(self, spec: PolicySpec, base: str | None = None) -> None:
        self.spec = spec
        self.base = base or spec.project_dir
        self.home = spec.home
        self.project = {
            _fold(os.path.normpath(spec.project_dir)),
            _fold(os.path.realpath(spec.project_dir)),
        }
        self.read_roots = [
            r for root in spec.read_roots for r in norm_path(root, spec.project_dir, spec.home)
        ]

    def forms(self, raw: str) -> tuple[str, str]:
        return norm_path(raw, self.base, self.home)

    def never(self, raw: str) -> str | None:
        for form in self.forms(raw):
            hit = _never_match(form, self.spec.never_paths, self.home)
            if hit:
                return hit
        return None

    def in_project(self, raw: str) -> bool:
        lexical, resolved = self.forms(raw)
        # both forms must stay inside: blocks ../ tricks and symlinks pointing out
        return any(within(lexical, p) for p in self.project) and any(
            within(resolved, p) for p in self.project
        )

    def readable(self, raw: str) -> bool:
        lexical, resolved = self.forms(raw)
        return any(within(lexical, r) for r in self.read_roots) and any(
            within(resolved, r) for r in self.read_roots
        )

    def protected(self, raw: str) -> str | None:
        _, resolved = self.forms(raw)
        for proj in self.project:
            for d in PROTECTED_PROJECT_DIRS:
                if within(resolved, f"{proj}/{d}"):
                    return d
        return None


# --------------------------------------------------------------------------- decide


def decide(spec: PolicySpec, tool: str, tool_input: dict[str, Any]) -> Decision:
    try:
        return _decide(spec, tool, tool_input or {})
    except Exception as e:  # fail closed
        return Decision("deny", f"policy error: {e}")


def _decide(spec: PolicySpec, tool: str, ti: dict[str, Any]) -> Decision:
    checker = _PathCheck(spec)
    tier_tools = READ_TIER_TOOLS if spec.tier == "read" else WORK_TIER_TOOLS

    if tool in READ_PATH_TOOLS:
        return _decide_read(checker, tool, ti)

    if tool in WEB_TOOLS:
        return Decision(
            "allow", "web access is in the Read tier; later shell commands need approval"
        )

    if tool in SAFE_TOOLS:
        return Decision("allow", "no side effects")

    if tool in EDIT_TOOLS:
        raw = str(ti.get(EDIT_TOOLS[tool]) or "")
        if not raw:
            return Decision("deny", "no path given")
        hit = checker.never(raw)
        if hit:
            return Decision("deny", f"Never rule: writing {raw} matches {hit}")
        prot = checker.protected(raw)
        if prot:
            return Decision("deny", f"Never rule: tasks may not write inside the project's {prot}/")
        if tool not in tier_tools:
            return Decision("ask", f"{tool} is not in the {spec.tier} tier")
        if not checker.in_project(raw):
            return Decision("ask", f"{raw} is outside the project folder")
        return Decision("allow", "edit inside the project")

    if tool in SHELL_TOOLS:
        return _decide_shell(spec, checker, str(ti.get("command") or ""), tier_tools, tool)

    return Decision("ask", f"tool {tool} is not in any tier")


def _decide_read(checker: _PathCheck, tool: str, ti: dict[str, Any]) -> Decision:
    raw = str(ti.get(READ_PATH_TOOLS[tool]) or "")
    targets = [raw] if raw else [checker.spec.project_dir]
    if tool == "Glob":
        pattern = str(ti.get("pattern") or "")
        if os.path.isabs(_expand(pattern, checker.home)) or pattern.startswith(("~", "..")):
            prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0] or "/"
            targets.append(prefix)
        elif ".." in pattern.replace("\\", "/").split("/"):
            return Decision("ask", "glob pattern climbs out of the folder")
    for t in targets:
        hit = checker.never(t)
        if hit:
            return Decision("deny", f"Never rule: {t} matches {hit}")
        if not checker.readable(t):
            return Decision("ask", f"{t} is outside the readable folders")
    if tool == "Grep" and (ti.get("output_mode") or "files_with_matches") == "content":
        secret = _secret_below(checker, targets[0])
        if secret:
            return Decision(
                "deny",
                f"Never rule: grep content over a folder holding {secret} could print secrets; "
                "grep a narrower path or use files_with_matches",
            )
    return Decision("allow", "read inside readable folders")


def _secret_below(checker: _PathCheck, raw: str) -> str | None:
    root = checker.forms(raw)[1]
    if os.path.isfile(root):
        return None
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _GREP_SKIP_DIRS]
        for f in filenames:
            seen += 1
            if seen > _GREP_SCAN_LIMIT:
                return None
            if _never_match(_fold(f"{dirpath}/{f}"), checker.spec.never_paths, checker.home):
                return f
    return None


# --------------------------------------------------------------------------- shell

_FORBIDDEN_SHELL = ("\n", "\r", "`", "$", ";", "<", "\x00")
_REDIRECT_OK = re.compile(r"(?<!\S)2>&1(?!\S)")
_PIPE_OK = {"head", "tail"}


def split_shell(command: str) -> list[list[str]] | str:
    """Split a command into ``&&``-joined segments, or return why it is not parseable.

    Only a tiny, predictable subset of shell is accepted: words, quotes, ``&&``, ``2>&1``,
    and a trailing ``| head``/``| tail``. Anything else (``;``, ``|`` to other programs,
    redirects, ``$`` expansion, backticks, subshells) is not allowlisted.
    """
    for ch in _FORBIDDEN_SHELL:
        if ch in command:
            return f"shell feature {ch!r} is not allowed without approval"
    cleaned = _REDIRECT_OK.sub(" ", command)
    try:
        lex = shlex.shlex(cleaned, posix=True, punctuation_chars="&|><()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:
        return f"cannot parse command: {e}"
    segments: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "&&":
            segments.append([])
        elif tok == "|":
            rest = tokens[i + 1 :]
            if not _is_head_tail(rest):
                return "pipes are only allowed into head or tail"
            break
        elif set(tok) <= set("&|><()"):
            return f"shell operator {tok!r} is not allowed without approval"
        else:
            segments[-1].append(tok)
        i += 1
    if any(not s for s in segments):
        return "empty command segment"
    return segments


def _is_head_tail(rest: list[str]) -> bool:
    if not rest or program_name(rest[0]) not in _PIPE_OK:
        return False
    args = rest[1:]
    if args[:1] == ["-n"] and len(args) == 2 and args[1].isdigit():
        return True
    return not args or (len(args) == 1 and re.fullmatch(r"-\d+|-n\d+", args[0]) is not None)


def _decide_shell(
    spec: PolicySpec, checker: _PathCheck, command: str, tier_tools: frozenset[str], tool: str
) -> Decision:
    if not command.strip():
        return Decision("deny", "empty command")
    parsed = split_shell(command)
    never_programs = {program_name(p) for p in spec.never_programs}
    if isinstance(parsed, str):
        # Still refuse outright if a Never program is visible anywhere in the text.
        for word in re.split(r"[\s;&|()`$'\"]+", command):
            if word and program_name(word) in never_programs:
                return Decision("deny", f"Never rule: {program_name(word)} is never run")
        return Decision("ask", parsed)

    cwd = spec.project_dir
    pending: Decision | None = None
    for argv in parsed:
        prog = program_name(argv[0])
        if prog in never_programs:
            return Decision("deny", f"Never rule: {prog} is never run")
        seg_check = _PathCheck(spec, base=cwd)
        for arg in argv[1:]:
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if _looks_like_path(value):
                hit = seg_check.never(value)
                if hit:
                    return Decision("deny", f"Never rule: argument {value} matches {hit}")
                if not seg_check.in_project(value) and pending is None:
                    pending = Decision("ask", f"argument {value} points outside the project")
        if prog == "cd":
            target = argv[1] if len(argv) > 1 else spec.home
            if len(argv) > 2 or not seg_check.in_project(target):
                pending = pending or Decision("ask", "cd outside the project")
            else:
                cwd = seg_check.forms(target)[1]
            continue
        if pending is None and not _allowlisted(argv, spec.work_commands):
            pending = Decision("ask", f"`{' '.join(argv)}` is not on the Work allowlist")

    if tool not in tier_tools:
        return Decision("ask", f"shell commands are not in the {spec.tier} tier")
    if spec.is_web_tainted():
        return Decision("ask", "this task fetched web content, so shell commands need approval")
    return pending or Decision("allow", "allowlisted command")


def _looks_like_path(arg: str) -> bool:
    if not arg:
        return False
    a = arg.replace("\\", "/")
    return (
        "/" in a
        or a.startswith(("~", ".."))
        or (len(a) >= 2 and a[1] == ":" and a[0].isalpha())
        or _never_match(_fold(a), list(NEVER_PATHS), "~") is not None
    )


def _allowlisted(argv: list[str], allow: list[list[str]]) -> bool:
    for prefix in allow:
        if len(argv) < len(prefix):
            continue
        if program_name(argv[0]) != program_name(prefix[0]):
            continue
        if [a.lower() for a in argv[1 : len(prefix)]] == [p.lower() for p in prefix[1:]]:
            return True
    return False
