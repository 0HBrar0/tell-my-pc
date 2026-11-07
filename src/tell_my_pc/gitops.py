"""Git isolation: every task runs on its own ``tmp/task-<id>`` branch.

The report's list of changed files comes from ``git diff`` against the task's start commit,
never from what the model says it did.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BOT_NAME = "tell-my-pc"
BOT_EMAIL = "tell-my-pc@localhost"


# Build and cache output that running tests or builds leaves behind. The daemon never sweeps
# these into its leftover commit, even in repos without a .gitignore. (A task may still commit
# them deliberately with `git add`; then they show up in files_changed, because that is true.)
JUNK_EXCLUDES: tuple[str, ...] = tuple(
    f":(exclude,glob){p}"
    for p in (
        "**/__pycache__/**",
        "**/*.pyc",
        "**/.pytest_cache/**",
        "**/.ruff_cache/**",
        "**/.mypy_cache/**",
        "**/node_modules/**",
        "**/.DS_Store",
        "**/Thumbs.db",
        "**/.coverage",
        "**/htmlcov/**",
    )
)


class GitError(Exception):
    pass


@dataclass
class FileChange:
    status: str  # A, M, D, R, C, T
    path: str
    old_path: str | None = None

    def as_dict(self) -> dict[str, str]:
        d = {"status": self.status, "path": self.path}
        if self.old_path:
            d["old_path"] = self.old_path
        return d


@dataclass
class TaskBranch:
    project_dir: str
    branch: str | None
    """None when the task runs in place (Read tier on a dirty tree, or not a git repo)."""
    base_ref: str | None
    base_sha: str | None
    base_is_branch: bool = True
    start_untracked: set[str] = field(default_factory=set)
    note: str | None = None
    kept: bool = False


def git(cwd: str | Path, *args: str, check: bool = True) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def is_repo(path: str | Path) -> bool:
    """True only if ``path`` is the top of a git work tree.

    A folder that merely sits inside some other repository (for example a home folder that
    is itself a repo) does not count: switching branches there would touch the outer repo.
    """
    try:
        top = git(path, "rev-parse", "--show-toplevel", check=False).strip()
    except GitError:
        return False
    if not top:
        return False
    return os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(path))


def _identity_args(cwd: str) -> list[str]:
    if git(cwd, "config", "user.email", check=False).strip():
        return []
    return ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]


def _untracked(cwd: str) -> set[str]:
    out = git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    return {p for p in out.split("\0") if p}


def branch_exists(cwd: str, name: str) -> bool:
    return bool(git(cwd, "rev-parse", "--verify", "-q", f"refs/heads/{name}", check=False).strip())


def prepare(project_dir: str, task_id: str, tier: str, start_from: str | None = None) -> TaskBranch:
    """Create and switch to ``tmp/task-<id>``. Read tier may run in place instead."""
    if not is_repo(project_dir):
        if tier != "read":
            raise GitError(
                "This project is not a git repository, so changes could not be isolated. "
                "Run `git init` there first, or use the Read tier."
            )
        return TaskBranch(project_dir, None, None, None, note="not a git repo; ran read-only")

    if git(project_dir, "status", "--porcelain", "--untracked-files=no").strip():
        if tier != "read":
            raise GitError(
                "The project has uncommitted changes. Commit or stash them first, so the task's "
                "changes stay separate from yours."
            )
        head = git(project_dir, "rev-parse", "HEAD", check=False).strip() or None
        return TaskBranch(
            project_dir, None, None, head, note="uncommitted changes present; ran read-only"
        )

    base_ref = git(project_dir, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
    is_branch = bool(base_ref)
    head = git(project_dir, "rev-parse", "-q", "--verify", "HEAD", check=False).strip()
    if not head:
        raise GitError("The repository has no commits yet. Make a first commit, then retry.")
    if not is_branch:
        base_ref = head
    branch = f"tmp/task-{task_id}"
    untracked = _untracked(project_dir)
    if branch_exists(project_dir, branch):
        # A task that was held (usage limit) and now runs again: continue its own branch.
        base_sha = git(project_dir, "merge-base", "HEAD", branch).strip()
        git(project_dir, "switch", "-q", branch)
        return TaskBranch(project_dir, branch, base_ref, base_sha, is_branch, untracked)
    start = start_from if start_from and branch_exists(project_dir, start_from) else "HEAD"
    base_sha = git(project_dir, "rev-parse", start).strip()
    git(project_dir, "switch", "-q", "-c", branch, start)
    return TaskBranch(project_dir, branch, base_ref, base_sha, is_branch, untracked)


def _parse_name_status(out: str) -> list[FileChange]:
    parts = [p for p in out.split("\0") if p != ""]
    changes: list[FileChange] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        code = status[0]
        if code in "RC":
            changes.append(FileChange(code, parts[i + 2], old_path=parts[i + 1]))
            i += 3
        else:
            changes.append(FileChange(code, parts[i + 1]))
            i += 2
    return changes


def finalize(tb: TaskBranch, message: str) -> tuple[list[FileChange], list[str]]:
    """Commit leftovers on the task branch, diff it, and switch back to where the user was.

    Returns (files changed, problems). Never raises for ordinary git trouble: the report has
    to go out either way.
    """
    problems: list[str] = []
    cwd = tb.project_dir
    if tb.branch is None:
        # Ran in place: prove nothing changed.
        if not is_repo(cwd):
            return [], problems
        out = git(cwd, "status", "--porcelain", "-z", "--untracked-files=no", check=False)
        changed = [FileChange(e[:2].strip() or "M", e[3:]) for e in out.split("\0") if e]
        return changed, problems

    try:
        git(cwd, "add", "-A", "--", ".", *JUNK_EXCLUDES)
        keep_out = sorted(tb.start_untracked & _staged(cwd))
        for chunk in _chunks(keep_out, 100):
            git(cwd, "reset", "-q", "--", *chunk)
        if _staged(cwd):
            git(cwd, *_identity_args(cwd), "commit", "-q", "--no-verify", "-m", message)
    except GitError as e:
        problems.append(f"could not commit the task's changes: {e}")

    try:
        out = git(cwd, "diff", "--name-status", "-z", "-M", f"{tb.base_sha}..{tb.branch}")
        changes = _parse_name_status(out)
    except GitError as e:
        changes = []
        problems.append(f"could not diff the task branch: {e}")

    try:
        if tb.base_is_branch and tb.base_ref:
            git(cwd, "switch", "-q", tb.base_ref)
        elif tb.base_ref:
            git(cwd, "switch", "-q", "--detach", tb.base_ref)
    except GitError as e:
        problems.append(f"could not switch back to {tb.base_ref}: {e}")
        tb.kept = True
        return changes, problems

    ahead = git(cwd, "rev-list", "--count", f"{tb.base_sha}..{tb.branch}", check=False).strip()
    if ahead in ("", "0"):
        git(cwd, "branch", "-q", "-D", tb.branch, check=False)
        tb.kept = False
    else:
        tb.kept = True
    return changes, problems


def _staged(cwd: str) -> set[str]:
    out = git(cwd, "diff", "--cached", "--name-only", "-z")
    return {p for p in out.split("\0") if p}


def _chunks(items: list[str], n: int) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]
