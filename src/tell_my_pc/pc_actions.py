"""Curated computer actions for computer tasks (ADR 0008).

Each action does one fixed thing. Nothing here takes a free-form command: names and paths are
checked, and passed to PowerShell through environment variables, never spliced into a script.
The PreToolUse hook decides whether a call may run (``policy._decide_pc``); the MCP server
re-checks paths before calling these functions.

Windows is fully supported. On macOS and Linux, system info, URLs and opening files work; the
rest says it is not supported yet.
"""

from __future__ import annotations

import ctypes
import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

IS_WINDOWS = sys.platform == "win32"


class ActionRefused(Exception):
    """The action can't or won't run; the message is shown to the model."""


# Opening these is running code, not opening a document.
EXECUTABLE_EXTS = frozenset(
    [
        ".exe",
        ".com",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msi",
        ".msp",
        ".scr",
        ".pif",
        ".lnk",
        ".url",
        ".hta",
        ".cpl",
        ".jar",
        ".py",
        ".pyw",
        ".sh",
        ".reg",
        ".inf",
        ".appref-ms",
        ".application",
        ".gadget",
        ".msc",
        ".dll",
        ".sys",
        ".ocx",
        ".scf",
        ".library-ms",
        ".settingcontent-ms",
        ".appx",
        ".msix",
        ".appinstaller",
        ".iso",
        ".vhd",
        ".vhdx",
    ]
)

MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}
POWER_ACTIONS = ("sleep", "restart", "shutdown", "cancel")
_PROCESS_NAME = re.compile(r"^[\w .()+-]{1,100}$")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _need_windows(what: str) -> None:
    if not IS_WINDOWS:
        raise ActionRefused(f"{what} is only supported on Windows so far.")


def _powershell(script: str, env: dict[str, str] | None = None, timeout: float = 30) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, **(env or {})}, creationflags=_NO_WINDOW,
    )  # fmt: skip
    if r.returncode != 0:
        raise ActionRefused((r.stderr or r.stdout).strip()[:500] or "PowerShell failed")
    return r.stdout


# --------------------------------------------------------------------------- info


def system_info() -> str:
    lines = [
        f"Time: {time.strftime('%A %Y-%m-%d %H:%M')}",
        f"Computer: {platform.node()} ({platform.system()} {platform.release()})",
    ]
    if IS_WINDOWS:
        lines += _windows_power_memory_uptime()
    drives = os.listdrives() if IS_WINDOWS and hasattr(os, "listdrives") else ["/"]
    for d in drives:
        try:
            u = shutil.disk_usage(d)
        except OSError:
            continue
        lines.append(
            f"Disk {d}: {u.free / 1e9:.0f} GB free of {u.total / 1e9:.0f} GB "
            f"({100 * u.used / u.total:.0f}% used)"
        )
    return "\n".join(lines)


def _windows_power_memory_uptime() -> list[str]:
    out: list[str] = []

    class PowerStatus(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    ps = PowerStatus()
    if k32.GetSystemPowerStatus(ctypes.byref(ps)):
        if ps.BatteryFlag == 128 or ps.BatteryLifePercent == 255:
            out.append("Battery: none (desktop or not reported)")
        else:
            plugged = {0: "on battery", 1: "plugged in"}.get(ps.ACLineStatus, "power unknown")
            left = ""
            if ps.ACLineStatus == 0 and ps.BatteryLifeTime != 0xFFFFFFFF:
                left = (
                    f", about {ps.BatteryLifeTime // 3600}h {ps.BatteryLifeTime % 3600 // 60}m left"
                )
            out.append(f"Battery: {ps.BatteryLifePercent}% ({plugged}{left})")
    ms = MemoryStatus()
    ms.dwLength = ctypes.sizeof(MemoryStatus)
    if k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        out.append(
            f"Memory: {ms.dwMemoryLoad}% used, {ms.ullAvailPhys / 1e9:.1f} GB free of "
            f"{ms.ullTotalPhys / 1e9:.1f} GB"
        )
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    up = k32.GetTickCount64() // 1000
    out.append(f"Up for: {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m")
    return out


def list_processes(limit: int = 40) -> str:
    _need_windows("Listing processes")
    r = subprocess.run(
        ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30,
        check=False, creationflags=_NO_WINDOW,
    )  # fmt: skip
    totals: dict[str, tuple[int, int]] = {}
    for line in r.stdout.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) < 5:
            continue
        name = cols[0]
        kb = int(re.sub(r"\D", "", cols[4]) or 0)
        n, mem = totals.get(name, (0, 0))
        totals[name] = (n + 1, mem + kb)
    rows = sorted(totals.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    return "Programs by memory use:\n" + "\n".join(
        f"{name} ({n}x): {mem / 1024:.0f} MB" for name, (n, mem) in rows
    )


# --------------------------------------------------------------------------- apps


def _start_apps() -> list[dict[str, str]]:
    raw = _powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
    data = json.loads(raw or "[]")
    if isinstance(data, dict):
        data = [data]
    return [
        {"name": str(d.get("Name") or ""), "id": str(d.get("AppID") or "")}
        for d in data
        if d.get("Name") and d.get("AppID")
    ]


def list_apps(query: str = "") -> str:
    _need_windows("Listing apps")
    apps = _start_apps()
    if query:
        q = query.lower()
        apps = [a for a in apps if q in a["name"].lower()]
    names = sorted({a["name"] for a in apps}, key=str.lower)
    if not names:
        return "No matching apps in the Start menu."
    return f"{len(names)} apps:\n" + "\n".join(names[:300])


def find_app(name: str, apps: list[dict[str, str]]) -> dict[str, str] | list[str]:
    """The best Start-menu match, or a list of candidates when it is ambiguous."""
    q = name.strip().lower()
    if not q:
        return []
    exact = [a for a in apps if a["name"].lower() == q]
    if exact:
        return exact[0]
    starts = [a for a in apps if a["name"].lower().startswith(q)]
    contains = starts or [a for a in apps if q in a["name"].lower()]
    if len(contains) == 1:
        return contains[0]
    if contains:
        return sorted({a["name"] for a in contains})[:10]
    close = difflib.get_close_matches(q, [a["name"].lower() for a in apps], n=5, cutoff=0.6)
    hits = [a for a in apps if a["name"].lower() in close]
    if len(hits) == 1:
        return hits[0]
    return sorted({a["name"] for a in hits})


def open_app(name: str) -> str:
    _need_windows("Opening apps")
    found = find_app(name, _start_apps())
    if isinstance(found, list):
        if not found:
            raise ActionRefused(f"No app called {name!r} in the Start menu.")
        raise ActionRefused(f"{name!r} matches several apps: {', '.join(found)}. Pick one.")
    # Launch by AppID through the shell, exactly as the Start menu does. AppIDs come from
    # Get-StartApps, not from the model.
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{found['id']}"], creationflags=_NO_WINDOW)
    return f"Opened {found['name']}."


def close_app(name: str) -> str:
    _need_windows("Closing apps")
    exe = name.strip()
    if not _PROCESS_NAME.match(exe):
        raise ActionRefused("Give the program's process name, like spotify.exe.")
    if not exe.lower().endswith(".exe"):
        exe += ".exe"
    # Without /f: programs get the normal close request and may ask to save.
    r = subprocess.run(
        ["taskkill", "/im", exe], capture_output=True, text=True, timeout=30, check=False,
        creationflags=_NO_WINDOW,
    )  # fmt: skip
    if r.returncode != 0:
        raise ActionRefused((r.stderr or r.stdout).strip()[:300])
    return f"Asked {exe} to close."


# --------------------------------------------------------------------------- open


def open_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ActionRefused("Only http:// and https:// links can be opened.")
    if IS_WINDOWS:
        os.startfile(u.geturl())  # type: ignore[attr-defined]  # noqa: S606 (checked http(s) URL)
    else:
        webbrowser.open(u.geturl())
    return f"Opened {u.netloc} in the browser."


def open_path(path: Path) -> str:
    if not path.exists():
        raise ActionRefused(f"{path} does not exist.")
    if path.is_file():
        ext = "".join(path.suffixes[-1:]).lower()
        if ext in EXECUTABLE_EXTS or not ext:
            raise ActionRefused(
                f"{path.name} is a program, script or shortcut. Opening it would run code, so "
                "it needs to be done another way (with your approval)."
            )
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 (no executables)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    return f"Opened {path}."


# --------------------------------------------------------------------------- keys, lock


def media(action: str, times: int = 1) -> str:
    _need_windows("Media keys")
    if action == "set_volume":
        level = max(0, min(100, times))
        _press(MEDIA_KEYS["volume_down"], 50)  # each step is 2%
        _press(MEDIA_KEYS["volume_up"], round(level / 2))
        return f"Volume set to about {level}%."
    if action not in MEDIA_KEYS:
        raise ActionRefused(f"Unknown media action {action!r}.")
    n = max(1, min(50, times))
    _press(MEDIA_KEYS[action], n)
    return f"Pressed {action.replace('_', ' ')}" + (f" {n} times." if n > 1 else ".")


def _press(vk: int, n: int) -> None:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for _ in range(n):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.02)


def lock_screen() -> str:
    _need_windows("Locking the screen")
    if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
        raise ActionRefused("Windows refused to lock the screen.")
    return "Screen locked."


def power(action: str) -> str:
    _need_windows("Power actions")
    if action not in POWER_ACTIONS:
        raise ActionRefused(f"Power action must be one of {', '.join(POWER_ACTIONS)}.")
    if action == "sleep":
        # Sleeping right away would cut off the report; start it a little later.
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Start-Sleep 20; & rundll32.exe powrprof.dll,SetSuspendState 0,1,0"],
            creationflags=_NO_WINDOW,
        )  # fmt: skip
        return "The computer will go to sleep in about 20 seconds."
    if action == "cancel":
        argv = ["shutdown", "/a"]
    else:
        # A minute's delay: the report still reaches the phone, and `cancel` can stop it.
        argv = ["shutdown", "/r" if action == "restart" else "/s", "/t", "60"]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        raise ActionRefused((r.stderr or r.stdout).strip()[:300] or f"{action} failed")
    return {
        "cancel": "Cancelled the pending restart or shutdown.",
        "restart": "Restarting in 60 seconds (say 'cancel the restart' to stop it).",
        "shutdown": "Shutting down in 60 seconds (say 'cancel the shutdown' to stop it).",
    }[action]


# --------------------------------------------------------------------------- files


def screenshot(folder: Path) -> str:
    _need_windows("Screenshots")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
        "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
        "$bmp.Save($env:TMP_OUT, [System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose()"
    )
    _powershell(script, env={"TMP_OUT": str(out)})
    return f"Saved {out}. Use deliver_file to send it to the phone."


def recycle(path: Path) -> str:
    _need_windows("The Recycle Bin")
    if not path.exists():
        raise ActionRefused(f"{path} does not exist.")
    kind = "DeleteDirectory" if path.is_dir() else "DeleteFile"
    script = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{kind}($env:TMP_PATH, "
        "'OnlyErrorDialogs', 'SendToRecycleBin')"
    )
    _powershell(script, env={"TMP_PATH": str(path)})
    return f"Moved {path} to the Recycle Bin."
