from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from tell_my_pc.config import Config
from tell_my_pc.daemon import Daemon
from tell_my_pc.plugins.api import IncomingTask
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.store import Store

from .conftest import git
from .helpers import RecordingNotifier

MakeDaemon = Callable[..., Daemon]


@pytest.fixture
async def make_daemon(cfg_dict: dict, tmp_path: Path) -> AsyncIterator[MakeDaemon]:
    made: list[Daemon] = []

    def factory(script: list[dict[str, Any]] | None = None, **cfg_over: Any) -> Daemon:
        d = dict(cfg_dict)
        d.update(cfg_over)
        cfg = Config.model_validate(d)
        store = Store(tmp_path / "data" / "state.db")
        daemon = Daemon(cfg, store, FakeExecutor(script), RecordingNotifier(), tmp_path / "data")
        made.append(daemon)
        return daemon

    yield factory
    for d in made:
        await d.close()
        d.store.close()


def notes(d: Daemon) -> RecordingNotifier:
    assert isinstance(d.notifier, RecordingNotifier)
    return d.notifier


async def test_run_tests_end_to_end(make_daemon: MakeDaemon, repo: Path) -> None:
    d = make_daemon(
        [
            {"tool": "Bash", "input": {"command": "pytest -q"}},
            {"tool": "Write", "input": {"file_path": "CHANGELOG.md", "content": "tests pass\n"}},
            {"say": "All tests pass."},
        ]
    )
    await d.start()
    r = await d.submit(IncomingTask("Run the tests and fix the failures.", source="test"))
    assert r.ok and "demo" in r.speak
    await d.wait_idle()
    t = d.store.get_task(r.data["id"])
    assert t and t.status == "done"
    result = t.result or {}
    assert result["files_changed"] == [{"status": "A", "path": "CHANGELOG.md"}]
    assert any("passed" in x for x in result["done"])
    assert result["branch"] == f"tmp/task-{t.id}" and result["branch_kept"]
    # user is back on main, which is untouched
    assert git(repo, "branch", "--show-current") == "main"
    assert not (repo / "CHANGELOG.md").exists()
    # report saved, notification sent
    md = Path(t.report_path or "")
    assert md.exists() and "CHANGELOG.md" in md.read_text(encoding="utf-8")
    sent = notes(d).sent
    assert sent and sent[-1].kind == "report" and "All tests pass." in sent[-1].message


async def test_files_changed_ignore_model_claims(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"say": "I changed 40 files."}])
    await d.start()
    r = await d.submit(IncomingTask("do nothing really"))
    await d.wait_idle()
    t = d.store.get_task(r.data["id"])
    assert t and (t.result or {})["files_changed"] == []
    assert (t.result or {})["branch_kept"] is False


async def test_denied_action_reported(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"tool": "Read", "input": {"file_path": "~/.ssh/id_rsa"}}])
    await d.start()
    r = await d.submit(IncomingTask("read my key"))
    await d.wait_idle()
    t = d.store.get_task(r.data["id"])
    res = t.result if t else None
    assert res and res["not_done"] and "Never rule" in res["not_done"][0]["reason"]


async def test_dirty_tree_refused_for_work(make_daemon: MakeDaemon, repo: Path) -> None:
    (repo / "README.md").write_text("uncommitted\n")
    d = make_daemon()
    await d.start()
    r = await d.submit(IncomingTask("fix things"))
    await d.wait_idle()
    t = d.store.get_task(r.data["id"])
    assert t and t.status == "refused"
    assert "uncommitted" in notes(d).sent[-1].message
    assert (repo / "README.md").read_text() == "uncommitted\n"


async def test_one_task_per_project(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"sleep": 0.5}, {"say": "ok"}])
    await d.start()
    r1 = await d.submit(IncomingTask("first"))
    r2 = await d.submit(IncomingTask("second"))
    assert "after the current task" in r2.speak or "Starting" in r2.speak
    await asyncio.sleep(0.2)
    t1, t2 = d.store.get_task(r1.data["id"]), d.store.get_task(r2.data["id"])
    assert t1 and t2 and t1.status == "running" and t2.status == "queued"
    await d.wait_idle()
    t2 = d.store.get_task(r2.data["id"])
    assert t2 and t2.status == "done"


async def test_parallel_limit_across_projects(
    make_daemon: MakeDaemon, cfg_dict: dict, tmp_path: Path
) -> None:
    projects = []
    for name in ("p1", "p2", "p3"):
        p = tmp_path / name
        p.mkdir()
        git(p, "init", "-q", "-b", "main")
        (p / "f").write_text("x")
        git(p, "add", "-A")
        git(p, "commit", "-q", "-m", "i")
        projects.append({"name": name, "path": str(p)})
    d = make_daemon([{"sleep": 0.6}], projects=projects, limits={"max_parallel_projects": 2})
    await d.start()
    for name in ("p1", "p2", "p3"):
        await d.submit(IncomingTask("work", project=name))
    await asyncio.sleep(0.3)
    assert len(d.store.tasks(("running",))) == 2
    await d.wait_idle()
    assert len(d.store.tasks(("done",))) == 3


async def test_stop_running_task(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"sleep": 10}])
    await d.start()
    r = await d.submit(IncomingTask("long thing"))
    await asyncio.sleep(0.3)
    s = await d.stop()
    assert s.ok and r.data["id"] in s.data["ids"]
    await d.wait_idle(timeout=20)
    t = d.store.get_task(r.data["id"])
    assert t and t.status == "stopped"


async def test_continue_resumes_project_session(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"tool": "Write", "input": {"file_path": "a.txt", "content": "1"}}])
    await d.start()
    r1 = await d.submit(IncomingTask("start the website"))
    await d.wait_idle()
    r2 = await d.submit(IncomingTask("Continue what you were doing"))
    assert r2.data["resume"] is True
    await d.wait_idle()
    ex = d.executor
    assert isinstance(ex, FakeExecutor)
    assert ex.calls[1].resume_session_id == f"fake-session-{r1.data['id']}"
    t2 = d.store.get_task(r2.data["id"])
    assert t2 and (t2.result or {})["resumed"] is True


async def test_continue_without_session_starts_fresh(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"say": "hi"}])
    await d.start()
    r = await d.submit(IncomingTask("", resume=True))
    await d.wait_idle()
    t = d.store.get_task(r.data["id"])
    assert t and "started fresh" in json.dumps(t.result)


async def test_unknown_project_asks_back(
    make_daemon: MakeDaemon, cfg_dict: dict, tmp_path: Path, repo: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    d = make_daemon(
        projects=[{"name": "website", "path": str(repo)}, {"name": "backend", "path": str(other)}]
    )
    await d.start()
    r = await d.submit(IncomingTask("fix the bug", project="banana"))
    assert not r.ok and "Which project" in r.speak
    assert notes(d).sent[-1].kind == "question"


def _two_projects(repo: Path, tmp_path: Path) -> list[dict[str, Any]]:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "f").write_text("x")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "i")
    return [
        {"name": "website", "path": str(repo), "aliases": ["homepage"]},
        {"name": "watcher simulator", "path": str(other), "aliases": ["simulator"]},
    ]


async def test_answer_which_project_by_saying_the_name(
    make_daemon: MakeDaemon, tmp_path: Path, repo: Path
) -> None:
    d = make_daemon([{"say": "ok"}], projects=_two_projects(repo, tmp_path))
    await d.start()
    r1 = await d.submit(IncomingTask("what still needs doing?"))
    assert not r1.ok and "Just say the project name" in r1.speak
    r2 = await d.submit(IncomingTask("the simulator project"))
    assert r2.ok and r2.data["project"] == "watcher simulator"
    await d.wait_idle()
    t = d.store.get_task(r2.data["id"])
    assert t and t.text == "what still needs doing?"
    kinds = [a["kind"] for a in d.store.audit_log()]
    assert "project_asked" in kinds and "project_answered" in kinds


async def test_pending_question_expires_and_is_single_use(
    make_daemon: MakeDaemon, tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tell_my_pc.daemon as D

    d = make_daemon([{"say": "ok"}], projects=_two_projects(repo, tmp_path))
    await d.start()
    monkeypatch.setattr(D, "PENDING_ASK_S", -1)
    await d.submit(IncomingTask("fix the bug"))
    r = await d.submit(IncomingTask("website"))
    # expired: "website" is treated as a new task on the website project
    assert r.ok and d.store.get_task(r.data["id"]).text == "website"  # type: ignore[union-attr]
    monkeypatch.setattr(D, "PENDING_ASK_S", 600)
    await d.submit(IncomingTask("fix the bug"))
    await d.submit(IncomingTask("Run the tests and fix the failures in the homepage"))
    r = await d.submit(IncomingTask("simulator"))  # question was replaced, not reused
    assert r.ok and d.store.get_task(r.data["id"]).text == "simulator"  # type: ignore[union-attr]
    await d.wait_idle()


async def test_status_speaks(make_daemon: MakeDaemon) -> None:
    d = make_daemon([{"say": "Everything is fine."}])
    await d.start()
    assert (await d.status()).speak == "No tasks yet."
    await d.submit(IncomingTask("check"))
    await d.wait_idle()
    s = await d.status()
    assert "Everything is fine." in s.speak


async def test_interrupted_tasks_are_reported_on_start(make_daemon: MakeDaemon) -> None:
    d = make_daemon()
    t = d.store.add_task("demo", "x", "work")
    d.store.update_task(t.id, status="running")
    await d.start()
    assert "interrupted" in notes(d).sent[0].title


async def test_empty_task_rejected(make_daemon: MakeDaemon) -> None:
    d = make_daemon()
    await d.start()
    r = await d.submit(IncomingTask("   "))
    assert not r.ok
