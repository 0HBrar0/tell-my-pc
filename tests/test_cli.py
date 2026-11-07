from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tell_my_pc.cli import main


def test_init_then_check(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "cfg" / "config.yaml"
    assert main(["--config", str(cfg), "init", "--project", str(repo), "--bind", "127.0.0.1"]) == 0
    out = capsys.readouterr().out
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    token = data["server"]["token"]["value"]
    assert len(token) >= 32 and token in out
    data["executor"] = {"type": "fake"}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["--config", str(cfg), "check"]) == 0
    assert "config OK" in capsys.readouterr().out
    # refuses to overwrite
    assert main(["--config", str(cfg), "init", "--project", str(repo)]) == 1


def test_check_refuses_unsafe(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "server": {"bind": "0.0.0." + "0"},
                "projects": [{"name": "x", "path": str(repo)}],
                "policy": {"builtin_never": False},
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(cfg), "check"]) == 1
    err = capsys.readouterr().err
    assert "all interfaces" in err and "Never deny list is empty" in err
    assert main(["--config", str(cfg), "serve"]) == 2


def test_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["paths"]) == 0
    assert "config:" in capsys.readouterr().out
