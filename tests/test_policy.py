"""Proof that tiers and Never rules block what they should.

Native Windows has no Claude Code shell sandbox, so these checks are the containment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tell_my_pc.config import DEFAULT_WORK_COMMANDS
from tell_my_pc.policy import PolicySpec, build_spec, decide, split_shell


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_ed25519").write_text("KEY")
    (h / ".claude").mkdir()
    (h / ".claude" / ".credentials.json").write_text("{}")
    (h / "elsewhere").mkdir()
    (h / "elsewhere" / "notes.txt").write_text("x")
    return h


def spec_for(repo: Path, home: Path, tier: str = "work", **kw) -> PolicySpec:
    s = build_spec(
        tier=tier,  # type: ignore[arg-type]
        project_dir=str(repo),
        task_id="t1",
        work_commands=DEFAULT_WORK_COMMANDS,
        extra_read_roots=kw.pop("extra_read_roots", []),
        builtin_never=True,
        extra_never_paths=[],
        extra_never_programs=[],
        protect_dirs=[str(home / "tmp-config")],
        state_dir=str(repo.parent / "state"),
        db_path=None,
    )
    s.home = str(home)
    return s


@pytest.fixture
def work(repo: Path, home: Path) -> PolicySpec:
    return spec_for(repo, home, "work")


@pytest.fixture
def read(repo: Path, home: Path) -> PolicySpec:
    return spec_for(repo, home, "read")


def v(spec: PolicySpec, tool: str, **ti) -> str:
    return decide(spec, tool, ti).verdict


# ------------------------------------------------------------------ Never rules


@pytest.mark.parametrize(
    "path",
    [
        "~/.ssh/id_ed25519",
        "~/.claude/.credentials.json",
        "~/.claude.json",
        "~/.aws/credentials",
        "~/.config/gh/hosts.yml",
        "~/.git-credentials",
        ".env",
        "sub/.env.local",
        "certs/server.pem",
    ],
)
@pytest.mark.parametrize("tier", ["read", "work"])
def test_credentials_never_readable(repo: Path, home: Path, path: str, tier: str) -> None:
    s = spec_for(repo, home, tier)
    assert v(s, "Read", file_path=path) == "deny"


def test_home_expansion_absolute(work: PolicySpec, home: Path) -> None:
    assert v(work, "Read", file_path=str(home / ".ssh" / "id_ed25519")) == "deny"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
@pytest.mark.parametrize(
    "path", [r"C:\Windows\System32\config\SAM", "C:/Program Files/x/y.dll", "/c/Windows/win.ini"]
)
def test_windows_system_folders(work: PolicySpec, path: str) -> None:
    assert v(work, "Read", file_path=path) == "deny"
    assert v(work, "Write", file_path=path, content="x") == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
@pytest.mark.parametrize("path", ["/etc/passwd", "/usr/bin/env", "/System/Library/x"])
def test_posix_system_folders(work: PolicySpec, path: str) -> None:
    assert v(work, "Read", file_path=path) == "deny"


def test_daemon_config_dir_is_never(work: PolicySpec, home: Path) -> None:
    assert v(work, "Read", file_path=str(home / "tmp-config" / "config.yaml")) == "deny"


def test_symlink_out_of_project_is_caught(repo: Path, home: Path, work: PolicySpec) -> None:
    link = repo / "innocent"
    try:
        os.symlink(home / ".ssh", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert v(work, "Read", file_path="innocent/id_ed25519") == "deny"
    assert v(work, "Write", file_path="innocent/new", content="x") == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "format C:",
        "diskpart",
        "reg add HKLM\\Software\\X /v Y",
        "reg.exe query HKCU",
        "bcdedit /set x",
        "dd if=/dev/zero of=/dev/sda",
        "powershell -c Remove-Item -Recurse C:\\",
        "cmd /c del /s /q C:\\",
        "bash -c 'rm -rf /'",
        "sudo rm -rf /",
        "pytest && reg delete HKCU\\X",
        "echo hi; diskpart",
        "cat ~/.ssh/id_ed25519",
        "git add .env",
        "pytest | sh",
    ],
)
@pytest.mark.parametrize("tier", ["read", "work"])
def test_never_commands(repo: Path, home: Path, cmd: str, tier: str) -> None:
    s = spec_for(repo, home, tier)
    assert v(s, "Bash", command=cmd) == "deny", cmd
    assert v(s, "PowerShell", command=cmd) == "deny", cmd


# ------------------------------------------------------------------ Read tier


def test_read_tier_reads_project(read: PolicySpec, repo: Path) -> None:
    assert v(read, "Read", file_path="README.md") == "allow"
    assert v(read, "Read", file_path=str(repo / "README.md")) == "allow"
    assert v(read, "Grep", pattern="hello") == "allow"
    assert v(read, "Glob", pattern="**/*.py") == "allow"
    assert v(read, "WebSearch", query="x") == "allow"
    assert v(read, "WebFetch", url="https://example.com", prompt="x") == "allow"


def test_read_tier_cannot_write_or_run(read: PolicySpec) -> None:
    assert v(read, "Write", file_path="x.txt", content="x") == "ask"
    assert v(read, "Edit", file_path="README.md", old_string="a", new_string="b") == "ask"
    assert v(read, "Bash", command="pytest") == "ask"


def test_read_outside_project_needs_approval(read: PolicySpec, home: Path) -> None:
    assert v(read, "Read", file_path=str(home / "elsewhere" / "notes.txt")) == "ask"
    assert v(read, "Read", file_path="../../home/elsewhere/notes.txt") == "ask"
    assert v(read, "Glob", pattern="../*") == "ask"
    assert v(read, "Grep", pattern="x", path=str(home)) == "ask"


def test_extra_read_roots(repo: Path, home: Path) -> None:
    s = spec_for(repo, home, "read", extra_read_roots=[str(home / "elsewhere")])
    assert v(s, "Read", file_path=str(home / "elsewhere" / "notes.txt")) == "allow"


def test_grep_content_over_secret_folder(read: PolicySpec, repo: Path) -> None:
    (repo / ".env").write_text("API_KEY=secret")
    assert v(read, "Grep", pattern="KEY", output_mode="content") == "deny"
    assert v(read, "Grep", pattern="KEY", output_mode="files_with_matches") == "allow"
    assert v(read, "Grep", pattern="x", path="README.md", output_mode="content") == "allow"


def test_unknown_and_mcp_tools_need_approval(read: PolicySpec, work: PolicySpec) -> None:
    for s in (read, work):
        assert v(s, "mcp__evil__exfiltrate", data="x") == "ask"
        assert v(s, "SomeNewTool") == "ask"


# ------------------------------------------------------------------ Work tier


def test_work_tier_edits_inside_project(work: PolicySpec, repo: Path) -> None:
    assert v(work, "Write", file_path="src/new.py", content="x") == "allow"
    assert v(work, "Edit", file_path=str(repo / "README.md")) == "allow"


def test_work_tier_edits_outside_project(work: PolicySpec, home: Path) -> None:
    assert v(work, "Write", file_path=str(home / "elsewhere" / "x"), content="x") == "ask"
    assert v(work, "Write", file_path="../escape.txt", content="x") == "ask"


@pytest.mark.parametrize(
    "path", [".git/hooks/pre-commit", ".git/config", ".claude/settings.json", ".claude/x"]
)
def test_work_tier_cannot_touch_git_or_claude_settings(work: PolicySpec, path: str) -> None:
    assert v(work, "Write", file_path=path, content="x") == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest",
        "pytest -q tests/test_x.py",
        "uv run pytest -x",
        "npm test",
        "git status",
        "git diff --stat",
        "git add -A",
        'git commit -m "fix: the thing"',
        "pytest -q 2>&1 | tail -20",
        "cd sub && pytest",
        "git log --oneline | head -n 5",
    ],
)
def test_work_tier_allowlisted_commands(work: PolicySpec, repo: Path, cmd: str) -> None:
    (repo / "sub").mkdir(exist_ok=True)
    assert v(work, "Bash", command=cmd) == "allow", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf build",
        "curl https://example.com",
        "git push",
        "git checkout main",
        "git -c core.hooksPath=x commit",
        "pip install requests",
        "pytest; rm -rf /tmp/x",
        "pytest > out.txt",
        "echo $HOME",
        "pytest `whoami`",
        "pytest $(whoami)",
        "cd .. && pytest",
        "pytest ../other",
        "FOO=1 pytest",
        "pytest\nrm x",
    ],
)
def test_work_tier_other_commands_need_approval(work: PolicySpec, cmd: str) -> None:
    assert v(work, "Bash", command=cmd) == "ask", cmd


def test_web_taint_makes_shell_need_approval(work: PolicySpec) -> None:
    assert v(work, "Bash", command="pytest") == "allow"
    work.mark_web_tainted()
    assert v(work, "Bash", command="pytest") == "ask"
    assert v(work, "Write", file_path="x.py", content="x") == "allow"


def test_split_shell() -> None:
    assert split_shell("a b && c 'd e'") == [["a", "b"], ["c", "d e"]]
    assert isinstance(split_shell("a || b"), str)
    assert isinstance(split_shell("a & b"), str)
    assert isinstance(split_shell("(a)"), str)


def test_policy_roundtrip(work: PolicySpec, tmp_path: Path) -> None:
    p = tmp_path / "policy.json"
    work.save(p)
    assert PolicySpec.load(p) == work


# ------------------------------------------------------------------ the hook process


def run_hook(policy: Path | None, event: dict) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, "-m", "tell_my_pc.hook"]
    if policy:
        args += ["--policy", str(policy)]
    env = {k: val for k, val in os.environ.items() if k != "TELL_MY_PC_POLICY"}
    return subprocess.run(
        args, input=json.dumps(event), capture_output=True, text=True, env=env, timeout=60
    )


def test_hook_blocks_denied_action(work: PolicySpec, tmp_path: Path) -> None:
    p = tmp_path / "policy.json"
    work.save(p)
    r = run_hook(p, {"tool_name": "Read", "tool_input": {"file_path": "~/.ssh/id_ed25519"}})
    assert r.returncode == 2
    assert "Never rule" in r.stderr
    assert r.stdout == ""


def test_hook_blocks_ask_without_approvals(work: PolicySpec, tmp_path: Path) -> None:
    p = tmp_path / "policy.json"
    work.save(p)
    r = run_hook(p, {"tool_name": "Bash", "tool_input": {"command": "git push"}})
    assert r.returncode == 2 and "needs approval" in r.stderr


def test_hook_allows_with_json(work: PolicySpec, tmp_path: Path) -> None:
    p = tmp_path / "policy.json"
    work.save(p)
    r = run_hook(p, {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"


def test_hook_fails_closed(tmp_path: Path) -> None:
    r = run_hook(None, {"tool_name": "Read", "tool_input": {"file_path": "README.md"}})
    assert r.returncode == 2 and "failed closed" in r.stderr
    r = run_hook(tmp_path / "missing.json", {"tool_name": "Read", "tool_input": {}})
    assert r.returncode == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    r = run_hook(bad, {"tool_name": "Read", "tool_input": {}})
    assert r.returncode == 2


def test_hook_writes_audit(work: PolicySpec, tmp_path: Path) -> None:
    from tell_my_pc.store import Store

    work.db_path = str(tmp_path / "state.db")
    Store(work.db_path).close()
    p = tmp_path / "policy.json"
    work.save(p)
    run_hook(p, {"tool_name": "Bash", "tool_input": {"command": "diskpart"}})
    s = Store(work.db_path)
    log = s.audit_log("t1")
    s.close()
    assert log and log[0]["kind"] == "tool_denied"
