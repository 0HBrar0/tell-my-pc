from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from tell_my_pc import autostart as A
from tell_my_pc.cli import autostart_script, main

from .conftest import TOKEN


class FakeRun:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        rc = 1 if any(word in argv for word in self.fail) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom" if rc else "")


@pytest.fixture
def odd_config(tmp_path: Path) -> Path:
    """A config path that needs escaping in every format."""
    cfg = tmp_path / "it's 100% $HOME" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"server: {{token: {{value: {TOKEN}}}}}\n", encoding="utf-8")
    return cfg


LINUX_PY = Path("/opt/py thon/bin/python3")  # backslashes when the tests run on Windows
MAC_PY = Path("/usr/local/bin/python3")


def linux_plan(action: str, cfg: Path, home: Path) -> A.Plan:
    return A.plan(
        action, cfg, platform="linux", home=home, python=LINUX_PY,
        path_env="/usr/bin:/home/me/.local/bin", has=lambda tool: True,
    )  # fmt: skip


def mac_plan(action: str, cfg: Path, home: Path) -> A.Plan:
    return A.plan(
        action, cfg, platform="darwin", home=home, python=MAC_PY,
        path_env="/opt/homebrew/bin:/usr/bin", uid=501,
    )  # fmt: skip


def test_systemd_unit_escapes_and_runs_serve(tmp_path: Path, odd_config: Path) -> None:
    p = linux_plan("install", odd_config, tmp_path / "home")
    [(path, unit)] = p.writes
    assert path == tmp_path / "home" / ".config" / "systemd" / "user" / "tell-my-pc.service"
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    py = A._systemd_escape(str(LINUX_PY))  # pyright: ignore[reportPrivateUsage]
    assert exec_line.startswith(f'ExecStart="{py}" -m tell_my_pc.cli')
    assert "100%% $$HOME" in exec_line  # no systemd specifier or variable expansion
    assert "it's" in exec_line and exec_line.count('"') % 2 == 0
    assert "serve --log-file" in exec_line and "--data-dir" in exec_line
    assert 'Environment="PATH=/usr/bin:/home/me/.local/bin"' in unit
    assert "WantedBy=default.target" in unit
    assert TOKEN not in unit and "dangerously" not in unit
    assert [c.argv[2:] for c in p.commands] == [
        ["daemon-reload"],
        ["enable", "--now", "tell-my-pc.service"],
    ]


def test_systemd_install_and_remove(tmp_path: Path, odd_config: Path) -> None:
    home = tmp_path / "home"
    run = FakeRun()
    rc, msg = A.execute(linux_plan("install", odd_config, home), run)
    unit = home / ".config" / "systemd" / "user" / "tell-my-pc.service"
    assert rc == 0 and unit.exists() and "installed and started" in msg
    run = FakeRun()
    rc, msg = A.execute(linux_plan("remove", odd_config, home), run)
    assert rc == 0 and not unit.exists() and msg == "removed"
    assert run.calls[0][2:] == ["disable", "--now", "tell-my-pc.service"]
    assert run.calls[-1][2:] == ["daemon-reload"]


def test_systemd_failures_are_reported(tmp_path: Path, odd_config: Path) -> None:
    rc, msg = A.execute(linux_plan("install", odd_config, tmp_path), FakeRun({"enable"}))
    assert rc == 1 and msg == "boom"
    rc, _ = A.execute(linux_plan("status", odd_config, tmp_path), FakeRun({"status"}))
    assert rc == 1
    no_systemd = A.plan("install", odd_config, platform="linux", has=lambda tool: False)
    rc, msg = A.execute(no_systemd, FakeRun())
    assert rc == 1 and "systemctl not found" in msg


def test_launchd_plist(tmp_path: Path, odd_config: Path) -> None:
    p = mac_plan("install", odd_config, tmp_path / "home")
    [(path, text)] = p.writes
    assert path == tmp_path / "home" / "Library" / "LaunchAgents" / "dev.tell-my-pc.plist"
    doc = plistlib.loads(text.encode("utf-8"))
    assert doc["Label"] == "dev.tell-my-pc" and doc["RunAtLoad"] is True
    args = doc["ProgramArguments"]
    assert args[:3] == [str(MAC_PY), "-m", "tell_my_pc.cli"]
    assert str(odd_config.resolve()) in args and "serve" in args
    assert doc["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}
    assert TOKEN not in text
    assert [c.argv for c in p.before] == [["launchctl", "bootout", "gui/501/dev.tell-my-pc"]]
    assert p.commands[0].argv == ["launchctl", "bootstrap", "gui/501", str(path)]


def test_launchd_install_reloads_and_remove_deletes(tmp_path: Path, odd_config: Path) -> None:
    home = tmp_path / "home"
    run = FakeRun({"bootout"})  # nothing loaded yet: bootout fails, which is fine
    rc, _ = A.execute(mac_plan("install", odd_config, home), run)
    plist = home / "Library" / "LaunchAgents" / "dev.tell-my-pc.plist"
    assert rc == 0 and plist.exists()
    assert [c[1] for c in run.calls] == ["bootout", "bootstrap"]
    rc, msg = A.execute(mac_plan("remove", odd_config, home), FakeRun())
    assert rc == 0 and not plist.exists() and msg == "removed"
    rc, msg = A.execute(mac_plan("status", odd_config, home), FakeRun({"print"}))
    assert rc == 1


def test_windows_plan_is_the_scheduled_task(odd_config: Path) -> None:
    p = A.plan("install", odd_config, platform="win32")
    [cmd] = p.commands
    assert cmd.argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert cmd.argv[-1] == autostart_script("install", odd_config)
    assert "Register-ScheduledTask" in p.describe() and not p.writes


def test_dry_run_prints_without_touching_anything(
    tmp_path: Path, odd_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--config", str(odd_config), "autostart", "install", "--dry-run"])
    out = capsys.readouterr().out
    assert rc in (0, 1)  # 1 only on a Linux box without systemd
    assert "tell_my_pc.cli" in out or "systemctl not found" in out
    described = linux_plan("install", odd_config, tmp_path / "h").describe()
    assert "# write " in described and "systemctl --user enable --now" in described
    assert not (tmp_path / "h").exists()
