from __future__ import annotations

from pathlib import Path

import pytest

from tell_my_pc import gitops as G

from .conftest import git


def test_task_branch_roundtrip(repo: Path) -> None:
    (repo / "untracked-before.txt").write_text("mine")
    tb = G.prepare(str(repo), "abc12345", "work")
    assert tb.branch == "tmp/task-abc12345"
    assert git(repo, "branch", "--show-current") == "tmp/task-abc12345"
    (repo / "README.md").write_text("changed\n")
    (repo / "new.py").write_text("x = 1\n")
    (repo / "test_ok.py").unlink()
    changes, problems = G.finalize(tb, "tell-my-pc: task abc12345")
    assert problems == []
    got = {(c.status, c.path) for c in changes}
    assert got == {("M", "README.md"), ("A", "new.py"), ("D", "test_ok.py")}
    # back where the user was, their untracked file untouched and not committed
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "untracked-before.txt").read_text() == "mine"
    assert "untracked-before.txt" not in git(repo, "show", "--name-only", "tmp/task-abc12345")
    assert tb.kept and G.branch_exists(str(repo), "tmp/task-abc12345")
    # main is unchanged
    assert (repo / "README.md").read_text() == "hello\n"


def test_files_changed_come_from_git_including_model_commits(repo: Path) -> None:
    tb = G.prepare(str(repo), "t2", "work")
    (repo / "a.txt").write_text("a")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "model commit")
    (repo / "b.txt").write_text("b")
    changes, _ = G.finalize(tb, "leftovers")
    assert {c.path for c in changes} == {"a.txt", "b.txt"}


def test_empty_branch_deleted(repo: Path) -> None:
    tb = G.prepare(str(repo), "t3", "read")
    changes, _ = G.finalize(tb, "nothing")
    assert changes == [] and not tb.kept
    assert not G.branch_exists(str(repo), "tmp/task-t3")


def test_dirty_tree_refuses_work_but_read_runs_in_place(repo: Path) -> None:
    (repo / "README.md").write_text("dirty\n")
    with pytest.raises(G.GitError, match="uncommitted"):
        G.prepare(str(repo), "t4", "work")
    tb = G.prepare(str(repo), "t4", "read")
    assert tb.branch is None and tb.note
    changes, _ = G.finalize(tb, "x")
    assert [c.path for c in changes] == ["README.md"]


def test_not_a_repo(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(G.GitError, match="not a git"):
        G.prepare(str(d), "t5", "work")
    assert G.prepare(str(d), "t5", "read").branch is None


def test_continue_starts_from_previous_task_branch(repo: Path) -> None:
    tb = G.prepare(str(repo), "t6", "work")
    (repo / "step1.txt").write_text("1")
    G.finalize(tb, "step 1")
    tb2 = G.prepare(str(repo), "t7", "work", start_from="tmp/task-t6")
    assert (repo / "step1.txt").exists()
    (repo / "step2.txt").write_text("2")
    changes, _ = G.finalize(tb2, "step 2")
    assert [c.path for c in changes] == ["step2.txt"]
