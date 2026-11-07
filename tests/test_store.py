from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tell_my_pc.plugins.api import api_compatible
from tell_my_pc.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_task_lifecycle(store: Store) -> None:
    t = store.add_task("demo", "run the tests", "work", source="test")
    assert t.status == "queued" and len(t.id) == 8
    store.update_task(t.id, status="running", branch=f"tmp/task-{t.id}")
    got = store.get_task(t.id)
    assert got and got.status == "running" and got.branch == f"tmp/task-{t.id}"
    assert [x.id for x in store.tasks(("running",))] == [t.id]
    assert store.latest_task("demo").id == t.id  # type: ignore[union-attr]


def test_update_rejects_unknown_columns(store: Store) -> None:
    t = store.add_task("demo", "x", "read")
    with pytest.raises(ValueError):
        store.update_task(t.id, text="changed")


def test_interrupted_on_restart(tmp_path: Path) -> None:
    s = Store(tmp_path / "s.db")
    t = s.add_task("demo", "x", "read")
    s.update_task(t.id, status="running")
    s.close()
    s2 = Store(tmp_path / "s.db")
    assert s2.mark_interrupted() == [t.id]
    assert s2.get_task(t.id).status == "interrupted"  # type: ignore[union-attr]
    s2.close()


def test_sessions(store: Store) -> None:
    assert store.get_session("demo") is None
    store.set_session("demo", "sess-1", "tmp/task-a", "a")
    store.set_session("demo", "sess-2", "tmp/task-b", "b")
    s = store.get_session("demo")
    assert s and s["session_id"] == "sess-2" and s["branch"] == "tmp/task-b"


def test_audit(store: Store) -> None:
    store.audit("tool_denied", {"tool": "Bash"}, task_id="abc")
    log = store.audit_log("abc")
    assert log[0]["kind"] == "tool_denied" and log[0]["detail"]["tool"] == "Bash"


def test_nonce_replay(store: Store) -> None:
    assert store.use_nonce("n1", 300)
    assert not store.use_nonce("n1", 300)
    assert store.use_nonce("n2", 300)


def test_approvals(store: Store) -> None:
    aid = store.add_approval("t1", "Bash", "git push", "not allowlisted", 60)
    assert [a["id"] for a in store.unnotified_approvals()] == [aid]
    # no token yet: phone answers are refused
    assert not store.decide_approval(aid, "approved", token_hash="abc")
    store.mark_approval_notified(aid, "abc")
    assert store.unnotified_approvals() == []
    assert not store.decide_approval(aid, "approved", token_hash="wrong")
    assert store.decide_approval(aid, "approved", token_hash="abc")
    assert not store.decide_approval(aid, "denied", token_hash="abc")  # answered once only
    assert store.get_approval(aid)["status"] == "approved"  # type: ignore[index]


def test_approvals_on_a_database_from_before_approvals(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool TEXT NOT NULL,"
        " detail TEXT NOT NULL, status TEXT NOT NULL, token_hash TEXT NOT NULL,"
        " created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_at REAL)"
    )
    c.commit()
    c.close()
    s = Store(db)
    aid = s.add_approval("t1", "Bash", "git branch", "not allowlisted", 60)
    assert s.unnotified_approvals()[0]["id"] == aid
    s.close()


def test_expired_approval_cannot_be_approved(store: Store) -> None:
    aid = store.add_approval("t1", "Bash", "x", "r", -1)
    store.mark_approval_notified(aid, "h")
    assert not store.decide_approval(aid, "approved", token_hash="h")


def test_lock(store: Store) -> None:
    assert store.locked() is None
    store.set_locked(True, "phone")
    assert store.locked()["reason"] == "phone"  # type: ignore[index]
    store.set_locked(False)
    assert store.locked() is None


def test_api_versions() -> None:
    assert api_compatible("0.1", "0.1")
    assert not api_compatible("0.2", "0.1")
    assert api_compatible("1.3", "1.0")
    assert not api_compatible("2.0", "1.0")
