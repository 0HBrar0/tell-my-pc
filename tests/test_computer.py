"""Computer tasks (ADR 0008): every allow and every block, proven."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tell_my_pc import computer, paths
from tell_my_pc import pc_actions as pc
from tell_my_pc.config import Config, computer_problems, safety_problems
from tell_my_pc.daemon import Daemon
from tell_my_pc.mcp_server import Server
from tell_my_pc.plugins.api import IncomingTask
from tell_my_pc.plugins.executors.base import system_prompt
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.policy import PolicySpec, build_spec, decide
from tell_my_pc.store import Store

from .helpers import RecordingNotifier


def _pc(name: str) -> str:
    return f"mcp__tell_my_pc__{name}"


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    d = {
        name: tmp_path / name for name in ("workspace", "downloads", "study", "secret", "elsewhere")
    }
    for p in d.values():
        p.mkdir()
    (d["downloads"] / "old.zip").write_text("zip")
    (d["study"] / "notes.pdf").write_text("pdf")
    (d["secret"] / "private.txt").write_text("private")
    (d["elsewhere"] / "thing.txt").write_text("x")
    return d


def make_spec(d: dict[str, Path], tmp_path: Path, pc_actions: bool = True) -> PolicySpec:
    return build_spec(
        tier="work",
        project_dir=str(d["workspace"]),
        task_id="t1",
        work_commands=["pytest"],
        extra_read_roots=[str(d["study"])],
        builtin_never=True,
        extra_never_paths=[str(d["secret"]) + "/**"],
        extra_never_programs=[],
        protect_dirs=[],
        state_dir=str(tmp_path / "state"),
        db_path=None,
        write_roots=[str(d["downloads"])],
        pc_actions=pc_actions,
    )


# --------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    ("tool", "where", "name", "verdict"),
    [
        ("Write", "workspace", "a.txt", "allow"),
        ("Write", "downloads", "sorted/a.txt", "allow"),
        ("Write", "study", "a.txt", "ask"),  # read-only folder
        ("Write", "elsewhere", "a.txt", "ask"),  # not listed: approval
        ("Write", "secret", "a.txt", "deny"),  # blocked: no approval can unlock it
        ("Write", "downloads", ".env", "deny"),  # built-in Never list still applies
        ("Read", "study", "notes.pdf", "allow"),
        ("Read", "downloads", "old.zip", "allow"),
        ("Read", "elsewhere", "thing.txt", "ask"),
        ("Read", "secret", "private.txt", "deny"),
    ],
)
def test_folder_rules(
    dirs: dict[str, Path], tmp_path: Path, tool: str, where: str, name: str, verdict: str
) -> None:
    spec = make_spec(dirs, tmp_path)
    key = "file_path"
    ti: dict[str, Any] = {key: str(dirs[where] / name), "content": "x"}
    assert decide(spec, tool, ti).verdict == verdict


def test_climbing_out_of_a_write_root_is_not_writable(dirs: dict[str, Path], tmp_path) -> None:
    spec = make_spec(dirs, tmp_path)
    sneaky = str(dirs["downloads"] / ".." / "elsewhere" / "x.txt")
    assert decide(spec, "Write", {"file_path": sneaky, "content": ""}).verdict == "ask"


@pytest.mark.parametrize(
    ("tool", "args", "verdict"),
    [
        ("pc_system_info", {}, "allow"),
        ("pc_open_app", {"name": "Spotify"}, "allow"),
        ("pc_open_url", {"url": "https://example.com"}, "allow"),
        ("pc_media", {"action": "play_pause"}, "allow"),
        ("pc_lock_screen", {}, "allow"),
        ("pc_screenshot", {}, "allow"),
        ("pc_close_app", {"name": "spotify.exe"}, "ask"),
        ("pc_power", {"action": "shutdown"}, "ask"),
        ("pc_power", {"action": "restart"}, "ask"),
    ],
)
def test_pc_action_rules(
    dirs: dict[str, Path], tmp_path: Path, tool: str, args: dict, verdict: str
) -> None:
    assert decide(make_spec(dirs, tmp_path), _pc(tool), args).verdict == verdict


@pytest.mark.parametrize(
    ("tool", "where", "name", "verdict"),
    [
        ("pc_recycle", "downloads", "old.zip", "allow"),
        ("pc_recycle", "workspace", "", "deny"),  # the whole workspace
        ("pc_recycle", "downloads", "", "deny"),  # the whole write root
        ("pc_recycle", "study", "notes.pdf", "ask"),
        ("pc_recycle", "secret", "private.txt", "deny"),
        ("pc_open_path", "study", "notes.pdf", "allow"),
        ("pc_open_path", "elsewhere", "thing.txt", "ask"),
        ("pc_open_path", "secret", "private.txt", "deny"),
    ],
)
def test_pc_path_rules(
    dirs: dict[str, Path], tmp_path: Path, tool: str, where: str, name: str, verdict: str
) -> None:
    path = str(dirs[where] / name) if name else str(dirs[where])
    assert decide(make_spec(dirs, tmp_path), _pc(tool), {"path": path}).verdict == verdict


def test_pc_actions_are_denied_in_project_tasks(dirs: dict[str, Path], tmp_path: Path) -> None:
    spec = make_spec(dirs, tmp_path, pc_actions=False)
    for tool in ("pc_system_info", "pc_open_app", "pc_power", "pc_recycle"):
        d = decide(spec, _pc(tool), {"path": str(dirs["downloads"] / "old.zip")})
        assert d.verdict == "deny", tool


def test_shell_in_computer_tasks(dirs: dict[str, Path], tmp_path: Path) -> None:
    spec = make_spec(dirs, tmp_path)
    assert decide(spec, "Bash", {"command": "pytest -q"}).verdict == "allow"
    # Anything not allowlisted shows the exact command on the phone.
    move = f"mv {dirs['downloads'] / 'old.zip'} {dirs['downloads'] / 'zips'}"
    assert decide(spec, "Bash", {"command": move}).verdict == "ask"
    secret = str(dirs["secret"])
    for cmd in (f"rm -rf {secret}", f'rm -rf "{secret}"', "cat " + secret.replace("\\", "/")):
        assert decide(spec, "Bash", {"command": cmd}).verdict == "deny", cmd
    ps = f"Remove-Item -Recurse '{secret}\\private.txt'"
    assert decide(spec, "PowerShell", {"command": ps}).verdict == "deny"


def test_program_symlinked_into_a_never_folder_still_runs(
    dirs: dict[str, Path], tmp_path: Path
) -> None:
    """macOS CI: a venv's python links into /Library, a Never folder. Running it is fine."""
    import os

    real = dirs["secret"] / "python3"
    real.write_text("")
    link = dirs["workspace"] / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks not permitted here")
    spec = make_spec(dirs, tmp_path)
    prog = str(link).replace("\\", "/")  # as Git Bash writes it
    spec.work_commands.append([prog, "-m", "pytest"])
    assert decide(spec, "Bash", {"command": f"{prog} -m pytest -q"}).verdict == "allow"
    # ...but naming the Never folder itself is still denied.
    assert decide(spec, "Bash", {"command": f"cat {real}"}).verdict == "deny"


# --------------------------------------------------------------------------- config


def computer_cfg(cfg_dict: dict, d: dict[str, Path], **over: Any) -> Config:
    c = {
        "enabled": True,
        "workspace": str(d["workspace"]),
        "write_roots": [str(d["downloads"])],
        "read_roots": [str(d["study"])],
        "blocked": [str(d["secret"])],
        **over,
    }
    return Config.model_validate({**cfg_dict, "computer": c})


def test_computer_config_is_safe(cfg_dict: dict, dirs: dict[str, Path]) -> None:
    assert safety_problems(computer_cfg(cfg_dict, dirs)) == []
    only_computer = computer_cfg({**cfg_dict, "projects": []}, dirs)
    assert safety_problems(only_computer) == []


@pytest.mark.parametrize(
    ("field", "value", "expect"),
    [
        ("workspace", "~", "whole home folder"),
        ("write_roots", ["/"], "whole drive"),
        ("write_roots", ["DATA"], "own config or data"),
        ("write_roots", ["SECRET"], "blocked"),
    ],
)
def test_unsafe_computer_folders_are_refused(
    cfg_dict: dict, dirs: dict[str, Path], field: str, value: Any, expect: str
) -> None:
    def fill(v: Any) -> Any:
        if isinstance(v, list):
            return [fill(x) for x in v]
        if v == "DATA":
            return str(paths.data_dir())
        if v == "SECRET":
            return str(dirs["secret"])
        if v == "/":
            return str(Path(dirs["workspace"].anchor))
        return v

    cfg = computer_cfg(cfg_dict, dirs, **{field: fill(value)})
    problems = computer_problems(cfg)
    assert any(expect in p for p in problems), problems


def test_system_folders_are_refused(
    cfg_dict: dict, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows = dirs["elsewhere"] / "Windows"
    fake_windows.mkdir()
    monkeypatch.setenv("SystemRoot", str(fake_windows))
    cfg = computer_cfg(cfg_dict, dirs, write_roots=[str(fake_windows / "System32")])
    assert any("system folder" in p for p in computer_problems(cfg))


# --------------------------------------------------------------------------- daemon


@pytest.fixture
async def pc_daemon(cfg_dict: dict, dirs: dict[str, Path], tmp_path: Path):
    made: list[Daemon] = []

    def factory(script: list[dict[str, Any]], **over: Any) -> Daemon:
        cfg = computer_cfg(cfg_dict, dirs, **over)
        store = Store(tmp_path / "data" / "state.db")
        d = Daemon(cfg, store, FakeExecutor(script), RecordingNotifier(), tmp_path / "data")
        made.append(d)
        return d

    yield factory
    for d in made:
        await d.close()
        d.store.close()


async def test_computer_task_end_to_end(pc_daemon, dirs: dict[str, Path]) -> None:
    ws, dl = dirs["workspace"], dirs["downloads"]
    d = pc_daemon(
        [
            {"tool": "Write", "input": {"file_path": str(dl / "zips" / "old.zip"), "content": "z"}},
            {"tool": "Write", "input": {"file_path": "tmp/t/helper.py", "content": "print(1)"}},
            {"tool": "Edit", "input": {"file_path": str(dl / "old.zip"),
                                       "old_string": "zip", "new_string": "ZIP"}},
            {"tool": "Read", "input": {"file_path": str(dirs["secret"] / "private.txt")}},
            {"tool": _pc("pc_open_app"), "input": {"name": "Spotify"}},
            {"say": "Opened Spotify and tidied Downloads."},
        ]
    )  # fmt: skip
    await d.start()
    reply = await d.submit(IncomingTask(text="open spotify and tidy my downloads"))
    assert reply.ok and "computer" in reply.speak
    await d.wait_idle()
    t = d.store.latest_task()
    assert t is not None and t.project == "computer" and t.status == "done"
    result = json.loads(t.result_json or "{}")
    changed = {(f["status"], Path(f["path"]).name) for f in result["files_changed"]}
    # From the folder snapshot, not from the model; scratch files are not reported.
    assert changed == {("A", "old.zip"), ("M", "old.zip")}
    assert result["target"] == "computer" and "2 files changed" in result["speak"]
    assert any("private.txt" in x for x in result["denied"])
    assert not (ws / "tmp" / t.id).exists()  # scratch folder cleared

    ex = d.executor
    assert isinstance(ex, FakeExecutor)
    req = ex.calls[0]
    assert req.cwd == str(ws.resolve()) and req.branch is None
    assert str(ws / "tmp") in req.instructions and "pc_recycle" in req.instructions
    spec = PolicySpec.load(req.policy_path)
    assert spec.pc_actions and spec.write_roots == [str(dl.resolve())]


async def test_named_projects_still_win(pc_daemon, dirs: dict[str, Path]) -> None:
    d = pc_daemon([{"say": "ok"}])
    reply = await d.submit(IncomingTask(text="run the tests", project="demo"))
    assert reply.ok and "demo" in reply.speak
    reply = await d.submit(IncomingTask(text="what's my battery on my pc"))
    assert reply.ok and "computer" in reply.speak


async def test_without_default_target_it_asks(pc_daemon) -> None:
    d = pc_daemon([{"say": "ok"}], default_target=False)
    reply = await d.submit(IncomingTask(text="open spotify"))
    assert not reply.ok and "computer" in reply.speak  # offered as a choice
    reply = await d.submit(IncomingTask(text="computer"))
    assert reply.ok and "computer" in reply.speak


# --------------------------------------------------------------------------- MCP server


def test_mcp_lists_pc_tools_only_for_computer_tasks(dirs: dict[str, Path], tmp_path) -> None:
    names = {t["name"] for t in Server(make_spec(dirs, tmp_path)).tools()}
    assert {"pc_open_app", "pc_power", "deliver_file"} <= names
    project = {t["name"] for t in Server(make_spec(dirs, tmp_path, pc_actions=False)).tools()}
    assert not any(n.startswith("pc_") for n in project)


def call(server: Server, name: str, args: dict[str, Any]) -> tuple[bool, str]:
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}}
    )  # fmt: skip
    assert resp is not None
    res = resp["result"]
    return not res["isError"], res["content"][0]["text"]


def test_mcp_server_rechecks_paths(dirs: dict[str, Path], tmp_path: Path) -> None:
    s = Server(make_spec(dirs, tmp_path))
    ok, text = call(s, "pc_recycle", {"path": str(dirs["secret"] / "private.txt")})
    assert not ok and "Never" in text
    ok, text = call(s, "pc_recycle", {"path": str(dirs["study"] / "notes.pdf")})
    assert not ok and "outside" in text
    (dirs["workspace"] / "setup.exe").write_text("MZ")
    ok, text = call(s, "pc_open_path", {"path": str(dirs["workspace"] / "setup.exe")})
    assert not ok and "run code" in text
    ok, text = call(s, "pc_open_url", {"url": "file:///C:/Windows/System32/cmd.exe"})
    assert not ok and "http" in text
    ok, text = call(s, "pc_system_info", {})
    assert ok and "Disk" in text
    assert (dirs["secret"] / "private.txt").exists() and (dirs["study"] / "notes.pdf").exists()


def test_mcp_server_refuses_pc_tools_in_project_tasks(dirs: dict[str, Path], tmp_path) -> None:
    ok, text = call(Server(make_spec(dirs, tmp_path, pc_actions=False)), "pc_system_info", {})
    assert not ok and "computer tasks" in text


# --------------------------------------------------------------------------- actions


APPS = [
    {"name": "Spotify", "id": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
    {"name": "Notepad", "id": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
    {"name": "Notepad++", "id": "{6D809377}\\Notepad++\\notepad++.exe"},
    {"name": "Visual Studio Code", "id": "Microsoft.VisualStudioCode"},
    {"name": "Visual Studio 2022", "id": "VisualStudio.17"},
]


def test_find_app() -> None:
    assert pc.find_app("spotify", APPS) == APPS[0]
    assert pc.find_app("Notepad", APPS) == APPS[1]  # exact beats prefix
    assert pc.find_app("visual studio", APPS) == ["Visual Studio 2022", "Visual Studio Code"]
    assert pc.find_app("spotfy", APPS) == APPS[0]  # dictation typo
    assert pc.find_app("photoshop", APPS) == []


def test_open_path_refuses_programs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(pc.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(pc.subprocess, "Popen", lambda argv, **kw: opened.append(argv[-1]))
    for name in ("x.exe", "x.bat", "x.ps1", "x.lnk", "x.cmd", "x.vbs", "x.msi", "noext"):
        (tmp_path / name).write_text("x")
        with pytest.raises(pc.ActionRefused):
            pc.open_path(tmp_path / name)
    (tmp_path / "doc.pdf").write_text("x")
    pc.open_path(tmp_path / "doc.pdf")
    assert opened == [str(tmp_path / "doc.pdf")]


def test_power_and_close_validate_arguments() -> None:
    with pytest.raises(pc.ActionRefused):
        pc.power("format-disk")
    with pytest.raises(pc.ActionRefused):
        pc.close_app("spotify.exe & del *")


def test_system_prompt_for_computer_tasks() -> None:
    text = system_prompt("work", None, "- Your workspace is W.")
    assert "Your workspace is W." in text and "git branch" not in text
    assert "structured report" in text


def test_snapshot_diff(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("1")
    (tmp_path / "b").write_text("1")
    before = computer.snapshot(tmp_path)
    assert before is not None
    (tmp_path / "a").write_text("22")
    (tmp_path / "b").unlink()
    (tmp_path / "c").write_text("1")
    after = computer.snapshot(tmp_path)
    assert after is not None
    got = {(c["status"], Path(c["path"]).name) for c in computer.diff(before, after)}
    assert got == {("M", "a"), ("D", "b"), ("A", "c")}
