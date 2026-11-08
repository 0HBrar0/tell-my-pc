"""Start the daemon when the user logs in, without admin rights, as the user.

* Windows: a Task Scheduler task with an at-logon trigger (Register-ScheduledTask; ``schtasks
  /SC ONLOGON`` would need admin rights).
* Linux: a ``systemd --user`` unit.
* macOS: a launchd LaunchAgent.

Every platform runs ``python -m tell_my_pc.cli --config ... --data-dir ... serve --log-file ...``
with explicit paths, so it does not depend on environment variables the service manager may
not have. Secrets are never written into unit files: env-var secrets must be set for the
user's session (see docs/setup/autostart.md).

:func:`plan` only describes what to do, so it can be printed (``--dry-run``) and tested on any
OS; :func:`execute` does it.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

TASK_NAME = "tell-my-pc"
SYSTEMD_UNIT = "tell-my-pc.service"
LAUNCHD_LABEL = "dev.tell-my-pc"


@dataclass
class Command:
    argv: list[str]
    check: bool = True  # False: a failure is fine (e.g. unloading something not loaded)


@dataclass
class Plan:
    platform: str
    before: list[Command] = field(default_factory=list)  # run before the file changes
    writes: list[tuple[Path, str]] = field(default_factory=list)
    deletes: list[Path] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)  # run after the file changes
    dirs: list[Path] = field(default_factory=list)  # folders the service needs to exist
    done: str = ""
    problem: str | None = None  # set when this machine can't do it

    def describe(self) -> str:
        out: list[str] = []
        if self.problem:
            out.append(f"! {self.problem}")
        out += [_line(c) for c in self.before]
        for p, text in self.writes:
            out += [f"# write {p}", text.rstrip("\n"), ""]
        out += [f"# delete {p}" for p in self.deletes]
        out += [_line(c) for c in self.commands]
        return "\n".join(out)


def _line(c: Command) -> str:
    if c.argv[:1] == ["powershell"]:
        return c.argv[-1]  # the script itself
    return " ".join(_sh_quote(a) for a in c.argv)


def _sh_quote(s: str) -> str:
    if s and all(ch.isalnum() or ch in "-_./=:@%+," for ch in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def serve_args(config: Path | None) -> list[str]:
    cfg = (config or paths.config_file()).resolve()
    data = paths.data_dir().resolve()
    return [
        "-m", "tell_my_pc.cli", "--config", str(cfg), "--data-dir", str(data),
        "serve", "--log-file", str(data / "daemon.log"),
    ]  # fmt: skip


# --- Windows -------------------------------------------------------------------------------


def windows_script(action: str, config: Path | None = None, python: Path | None = None) -> str:
    name = _ps_quote(TASK_NAME)
    if action == "remove":
        return f"Unregister-ScheduledTask -TaskName {name} -Confirm:$false; 'removed'"
    if action == "status":
        return (
            f"$t = Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; "
            "if ($t) { $i = $t | Get-ScheduledTaskInfo; "
            '"state: $($t.State)  last run: $($i.LastRunTime)  result: $($i.LastTaskResult)" }'
            " else { 'not installed' }"
        )
    exe = python or Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    cfg = (config or paths.config_file()).resolve()
    data = paths.data_dir().resolve()
    argline = (
        f'-m tell_my_pc.cli --config "{cfg}" --data-dir "{data}" '
        f'serve --log-file "{data / "daemon.log"}"'
    )
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            '$user = "$env:USERDOMAIN\\$env:USERNAME"',
            f"$a = New-ScheduledTaskAction -Execute {_ps_quote(str(runner))} "
            f"-Argument {_ps_quote(argline)} -WorkingDirectory {_ps_quote(str(data))}",
            "$t = New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$p = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive "
            "-RunLevel Limited",
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)",
            f"Register-ScheduledTask -TaskName {name} -Action $a -Trigger $t -Principal $p "
            "-Settings $s -Force | Out-Null",
            f"Start-ScheduledTask -TaskName {name}",
            "'installed and started'",
        ]
    )


# --- Linux (systemd --user) ----------------------------------------------------------------


def _systemd_escape(s: str) -> str:
    """Escape specifiers (%), variable expansion ($), quotes and backslashes."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")


def _systemd_arg(arg: str) -> str:
    s = _systemd_escape(arg)
    return f'"{s}"' if not s or any(ch in s for ch in " \t'\"") else s


def systemd_unit(config: Path | None, python: Path, path_env: str) -> str:
    argv = [str(python), *serve_args(config)]
    data = paths.data_dir().resolve()
    return "\n".join(
        [
            "# Written by `tell-my-pc autostart install`; undo with `... autostart remove`.",
            "[Unit]",
            "Description=tell-my-pc daemon",
            "After=network-online.target",
            "",
            "[Service]",
            "ExecStart=" + " ".join(_systemd_arg(a) for a in argv),
            # The user manager's PATH is minimal: pass ours so `claude` is found.
            f'Environment="PATH={_systemd_escape(path_env)}"',
            # A plain path setting: no quotes allowed, only specifiers need escaping.
            "WorkingDirectory=" + str(data).replace("%", "%%"),
            "Restart=on-failure",
            "RestartSec=10",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


# --- macOS (launchd) -----------------------------------------------------------------------


def launchd_plist(config: Path | None, python: Path, path_env: str) -> str:
    data = paths.data_dir().resolve()
    doc = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(python), *serve_args(config)],
        # launchd doesn't read the shell profile: pass PATH so `claude` is found.
        "EnvironmentVariables": {"PATH": path_env},
        "WorkingDirectory": str(data),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardErrorPath": str(data / "launchd.err.log"),
    }
    return plistlib.dumps(doc).decode("utf-8")


# --- planning and running ------------------------------------------------------------------


def plan(
    action: str,
    config: Path | None = None,
    *,
    platform: str | None = None,
    home: Path | None = None,
    python: Path | None = None,
    path_env: str | None = None,
    uid: int | None = None,
    has: Callable[[str], bool] | None = None,
) -> Plan:
    platform = platform or sys.platform
    home = home or Path.home()
    python = python or Path(sys.executable)
    path_env = path_env if path_env is not None else os.environ.get("PATH", "")
    has = has or (lambda tool: shutil.which(tool) is not None)

    if platform == "win32":
        script = windows_script(action, config, python)
        cmd = Command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        return Plan(platform, commands=[cmd])

    p = Plan(platform)
    if action == "install":
        p.dirs.append(paths.data_dir().resolve())
    if platform == "darwin":
        if uid is None:
            uid = getattr(os, "getuid", lambda: 0)()
        target = f"gui/{uid}"
        service = f"{target}/{LAUNCHD_LABEL}"
        plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if action == "install":
            p.before.append(Command(["launchctl", "bootout", service], check=False))
            p.writes.append((plist, launchd_plist(config, python, path_env)))
            p.commands.append(Command(["launchctl", "bootstrap", target, str(plist)]))
            p.done = f"installed and started ({plist})"
        elif action == "remove":
            p.before.append(Command(["launchctl", "bootout", service], check=False))
            p.deletes.append(plist)
            p.done = "removed"
        else:
            p.commands.append(Command(["launchctl", "print", service]))
        return p

    # Linux and other systemd systems.
    if not has("systemctl"):
        p.problem = (
            "systemctl not found. Start `tell-my-pc serve` from your desktop's autostart "
            "settings instead (docs/setup/autostart.md)."
        )
        return p
    ctl = ["systemctl", "--user"]
    unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    if action == "install":
        p.writes.append((unit, systemd_unit(config, python, path_env)))
        p.commands += [
            Command([*ctl, "daemon-reload"]),
            Command([*ctl, "enable", "--now", SYSTEMD_UNIT]),
        ]
        p.done = f"installed and started ({unit})"
    elif action == "remove":
        p.before.append(Command([*ctl, "disable", "--now", SYSTEMD_UNIT], check=False))
        p.deletes.append(unit)
        p.commands.append(Command([*ctl, "daemon-reload"]))
        p.done = "removed"
    else:
        p.commands.append(Command([*ctl, "status", "--no-pager", "--lines=5", SYSTEMD_UNIT]))
    return p


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def execute(p: Plan, run: Runner = _run) -> tuple[int, str]:
    """Do the plan. Returns (exit code, message for the user)."""
    if p.problem:
        return 1, p.problem
    for c in p.before:
        run(c.argv)  # stopping something that isn't running is fine
    for d in p.dirs:
        d.mkdir(parents=True, exist_ok=True)
    for path, text in p.writes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for path in p.deletes:
        path.unlink(missing_ok=True)
    out: list[str] = []
    for c in p.commands:
        r = run(c.argv)
        text = (r.stdout or r.stderr or "").strip()
        if r.returncode != 0 and c.check:
            return r.returncode, text or "not installed"
        if text:
            out.append(text)
    if p.done:
        out.append(p.done)
    return 0, "\n".join(out)
