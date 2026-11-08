from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tell_my_pc import paths
from tell_my_pc import setup_wizard as W
from tell_my_pc.cli import main
from tell_my_pc.config import Config, safety_problems

from .conftest import git


def make_repo(root: Path, name: str) -> Path:
    r = root / name
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "f").write_text("x")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "i")
    return r


@pytest.fixture(autouse=True)
def fake_screenshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shots = tmp_path / "Screenshots"
    shots.mkdir()
    monkeypatch.setattr(W, "screenshot_dirs", lambda: [shots])
    return shots


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    d = tmp_path / "code"
    make_repo(d, "watcher-simulator")
    make_repo(d, "unslop")
    (d / "not-a-repo").mkdir()
    return d


def setup_args(cfg: Path, projects_dir: Path, *extra: str) -> list[str]:
    return [
        "--config", str(cfg), "setup", "--projects-dir", str(projects_dir),
        "--bind", "127.0.0.1", "--no-open", "--no-autostart", *extra,
    ]  # fmt: skip


def test_setup_with_defaults_writes_a_safe_config(
    tmp_path: Path, projects_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = tmp_path / "cfg" / "config.yaml"
    assert main(setup_args(cfg_path, projects_dir, "--yes")) == 0
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg = Config.model_validate(data)
    assert safety_problems(cfg) == []
    assert [p.name for p in cfg.projects] == ["unslop", "watcher simulator"]
    assert cfg.projects[1].aliases == ["watcher-simulator"]
    assert cfg.server.public_url == "http://127.0.0.1:8787"
    assert len(cfg.server.token.resolve() or "") >= 32
    assert cfg.delivery.attach_max_mb == 0  # no ntfy token: links only
    assert cfg.delivery.roots == [str(tmp_path / "Screenshots")]
    assert cfg.computer.enabled and cfg.computer.workspace.endswith("tell-my-pc")
    page = paths.data_dir() / "phone-setup.html"
    text = page.read_text(encoding="utf-8")
    assert "<svg" in text and "http://127.0.0.1:8787/task" in text
    assert (cfg.server.token.resolve() or "") in text
    assert "Config written" in capsys.readouterr().out


def test_setup_interactive_choices(tmp_path: Path, projects_dir: Path) -> None:
    cfg_path = tmp_path / "c.yaml"
    # folder, project choice, "send files from Screenshots?", ntfy token env var, computer tasks
    answers = iter([str(projects_dir), "2", "n", "NTFY_TOKEN", "n"])

    import argparse

    args = argparse.Namespace(
        config=cfg_path, projects_dir=None, bind="127.0.0.1", port=8800, yes=False,
        force=False, no_open=True, no_autostart=True,
    )  # fmt: skip
    rc = W.run(args, read=lambda prompt: next(answers, ""))
    assert rc == 0
    cfg = Config.model_validate(yaml.safe_load(cfg_path.read_text(encoding="utf-8")))
    assert [p.name for p in cfg.projects] == ["watcher simulator"]
    assert cfg.server.port == 8800
    assert cfg.notifier is not None and cfg.notifier.token is not None  # type: ignore[union-attr]
    assert cfg.delivery.attach_max_mb == 2 and cfg.delivery.roots == []
    assert not cfg.computer.enabled  # answered no


def test_computer_defaults(tmp_path: Path) -> None:
    assert W.computer_defaults(tmp_path)["write_roots"] == []
    (tmp_path / "Downloads").mkdir()
    d = W.computer_defaults(tmp_path)
    assert d["workspace"] == str(tmp_path / "tell-my-pc")
    assert d["write_roots"] == [str(tmp_path / "Downloads")]


def test_setup_refuses_to_overwrite_without_force(tmp_path: Path, projects_dir: Path) -> None:
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("keep me", encoding="utf-8")
    assert main(setup_args(cfg_path, projects_dir, "--yes")) == 1
    assert cfg_path.read_text(encoding="utf-8") == "keep me"
    assert main(setup_args(cfg_path, projects_dir, "--yes", "--force")) == 0


def test_setup_refuses_unsafe_bind(tmp_path: Path, projects_dir: Path) -> None:
    cfg_path = tmp_path / "c.yaml"
    args = [a if a != "127.0.0.1" else "0.0.0." + "0" for a in setup_args(cfg_path, projects_dir)]
    assert main([*args, "--yes"]) == 1
    assert not cfg_path.exists()


def test_setup_needs_repos(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(setup_args(tmp_path / "c.yaml", empty, "--yes")) == 1


def test_setup_needs_tailscale_without_bind(
    tmp_path: Path, projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(W, "tailscale_ip", lambda: None)
    args = [
        "--config", str(tmp_path / "c.yaml"), "setup", "--projects-dir", str(projects_dir),
        "--yes", "--no-open", "--no-autostart",
    ]  # fmt: skip
    assert main(args) == 1
    monkeypatch.setattr(W, "tailscale_ip", lambda: "100.64.1.2")
    assert main(args) == 0
    cfg = Config.model_validate(yaml.safe_load((tmp_path / "c.yaml").read_text("utf-8")))
    assert cfg.server.bind == "tailscale" and cfg.server.public_url == "http://100.64.1.2:8787"


def test_find_repos_on_a_single_repo(tmp_path: Path) -> None:
    r = make_repo(tmp_path, "solo")
    assert W.find_repos(r) == [r.resolve()]
