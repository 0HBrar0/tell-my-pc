"""Computer tasks: run on the computer itself instead of inside a project (ADR 0008).

The daemon treats the computer as one more target. Instead of a git branch it gets a workspace
folder with a per-task ``tmp/<task-id>`` scratch folder (deleted afterwards), and instead of
``git diff`` the report's changed files come from a before/after snapshot of the folders the
task may change.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import ComputerConfig, ProjectConfig

SNAPSHOT_LIMIT = 100_000  # files per folder; ~35k files take 2-4 s warm on a normal disk
_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "$RECYCLE.BIN"}

Snapshot = dict[str, tuple[int, int]]


def as_project(c: ComputerConfig) -> ProjectConfig:
    return ProjectConfig(name=c.name, path=c.workspace, aliases=c.aliases, default_tier="work")


def expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser().resolve()


@dataclass
class ComputerRun:
    workspace: Path
    tmp: Path
    roots: list[Path]
    before: dict[str, Snapshot] = field(default_factory=dict)
    untracked: list[str] = field(default_factory=list)

    @classmethod
    def start(cls, c: ComputerConfig, task_id: str) -> ComputerRun:
        ws = expand(c.workspace)
        tmp = ws / "tmp" / task_id
        tmp.mkdir(parents=True, exist_ok=True)
        roots = [ws, *(expand(w) for w in c.write_roots)]
        run = cls(workspace=ws, tmp=tmp, roots=roots)
        for r in roots:
            snap = snapshot(r, skip=[ws / "tmp"])
            if snap is None:
                run.untracked.append(str(r))
            else:
                run.before[str(r)] = snap
        return run

    def changes(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for root, before in self.before.items():
            after = snapshot(Path(root), skip=[self.workspace / "tmp"])
            if after is None:
                self.untracked.append(root)
                continue
            out += diff(before, after)
        return sorted(out, key=lambda c: c["path"].lower())

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def snapshot(root: Path, skip: list[Path] | None = None) -> Snapshot | None:
    """Size and mtime of every file under ``root``; None if there are too many to track."""
    out: Snapshot = {}
    if not root.is_dir():
        return out
    skip_norm = {os.path.normcase(str(s)) for s in (skip or [])}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP and os.path.normcase(os.path.join(dirpath, d)) not in skip_norm
        ]
        for f in filenames:
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out[full] = (st.st_size, st.st_mtime_ns)
            if len(out) > SNAPSHOT_LIMIT:
                return None
    return out


def diff(before: Snapshot, after: Snapshot) -> list[dict[str, str]]:
    changes = [{"status": "A", "path": p} for p in after.keys() - before.keys()]
    changes += [{"status": "D", "path": p} for p in before.keys() - after.keys()]
    changes += [
        {"status": "M", "path": p} for p in before.keys() & after.keys() if before[p] != after[p]
    ]
    return changes


def instructions(c: ComputerConfig, run: ComputerRun) -> str:
    writable = "\n".join(f"  - {r}" for r in run.roots)
    readable = "\n".join(f"  - {expand(r)}" for r in c.read_roots) or "  - (none)"
    return f"""- This is a task on the user's computer itself, not in a code project. There is no
  git branch: changes are real, so be careful and do only what was asked.
- Your workspace is {run.workspace} (you fully control it). Put scripts and temporary files
  in {run.tmp}; that folder is deleted when the task ends. Keep anything the user should keep
  in the workspace.
- You may change files in:
{writable}
- You may read (not change):
{readable}
- Other folders need the user's approval on their phone, and some are blocked entirely.
- Prefer the tell_my_pc pc_* tools for actions on the computer: pc_open_app, pc_open_url,
  pc_open_path, pc_media (play/pause, next, volume), pc_lock_screen, pc_screenshot,
  pc_system_info, pc_list_apps, pc_list_processes, pc_recycle (delete to the Recycle Bin).
  pc_close_app and pc_power (sleep/restart/shutdown) ask the user first.
- To delete, use pc_recycle, never rm or del. Any other shell command needs the user's
  approval and shows them the exact command, so keep commands short and clear.
- Send files to the phone with deliver_file (for example after pc_screenshot)."""
