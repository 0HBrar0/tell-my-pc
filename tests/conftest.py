from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

TOKEN = "t" * 40

# Tests must never touch the real user config dir.
os.environ.setdefault("TELL_MY_PC_CONFIG_DIR", str(Path(__file__).parent / ".no-config"))


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELL_MY_PC_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TELL_MY_PC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TELL_MY_PC_TOKEN", TOKEN)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    # git identity for repos created in tests
    for k, v in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(k, v)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repo with one commit on 'main'."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "core.autocrlf", "false")
    (r / "README.md").write_text("hello\n")
    (r / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def cfg_dict(repo: Path) -> dict:
    return {
        "version": 1,
        "server": {"bind": "127.0.0.1", "port": 0},
        "projects": [{"name": "demo", "path": str(repo), "aliases": ["the demo"]}],
        "executor": {"type": "fake"},
    }
