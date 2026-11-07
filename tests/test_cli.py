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


def test_lock_unlock_audit(capsys: pytest.CaptureFixture[str]) -> None:
    from tell_my_pc import paths
    from tell_my_pc.store import Store

    assert main(["lock", "--reason", "test"]) == 0
    s = Store(paths.data_dir() / "state.db")
    assert s.locked()
    s.close()
    assert main(["audit"]) == 0
    assert "locked" in capsys.readouterr().out
    assert main(["unlock"]) == 0
    s = Store(paths.data_dir() / "state.db")
    assert s.locked() is None
    s.close()


def test_autostart_script(tmp_path: Path) -> None:
    from tell_my_pc.cli import autostart_script

    cfg = tmp_path / "it's here" / "config.yaml"
    script = autostart_script("install", cfg)
    assert "Register-ScheduledTask" in script and "-AtLogOn" in script
    assert "-RunLevel Limited" in script and "schtasks" not in script
    assert "--config" in script and "--data-dir" in script and "--log-file" in script
    assert "it''s here" in script  # single quotes escaped for PowerShell
    assert "dangerously" not in script
    assert "Unregister-ScheduledTask" in autostart_script("remove")
    assert "Get-ScheduledTask" in autostart_script("status")


def test_serve_survives_pythonw_without_console(repo: Path, tmp_path: Path) -> None:
    """Autostart runs under pythonw, where sys.stdout/stderr are None."""
    import socket
    import subprocess
    import sys

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "server": {"bind": "127.0.0.1", "port": port, "token": {"value": "t" * 40}},
                "projects": [{"name": "x", "path": str(repo)}],
                "executor": {"type": "fake"},
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / "d.log"
    code = (
        "import sys, threading, os; sys.stdout = None; sys.stderr = None; "
        "threading.Timer(8, lambda: os._exit(0)).start(); "
        "from tell_my_pc.cli import main; "
        f"main(['--config', r'{cfg}', '--data-dir', r'{tmp_path / 'data'}', "
        f"'serve', '--log-file', r'{log}'])"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "listening on" in log.read_text(encoding="utf-8")


def test_data_dir_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    d = tmp_path / "elsewhere"
    assert main(["--data-dir", str(d), "paths"]) == 0
    assert str(d.resolve()) in capsys.readouterr().out
