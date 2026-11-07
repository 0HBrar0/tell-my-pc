"""Phase 3 done criterion: an out-of-tree notifier plugin works with no core changes.

The sample plugin in examples/plugins/file-notifier is made discoverable the same way pip
would: its package plus a ``.dist-info`` with ``entry_points.txt`` on ``sys.path``.
"""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tell_my_pc.cli import build_notifier, main
from tell_my_pc.config import Config
from tell_my_pc.daemon import Daemon
from tell_my_pc.plugins import registry
from tell_my_pc.plugins.api import IncomingTask
from tell_my_pc.plugins.executors.fake import FakeExecutor
from tell_my_pc.store import Store

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "plugins" / "file-notifier" / "src" / "tmp_file_notifier"


def install_fake_dist(
    site: Path, dist: str, group: str, entries: dict[str, str], package: Path | None = None
) -> None:
    """Lay out an installed distribution like pip does (package + .dist-info)."""
    if package is not None:
        shutil.copytree(package, site / package.name)
    info = site / f"{dist.replace('-', '_')}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
    lines = "\n".join(f"{k} = {v}" for k, v in entries.items())
    (info / "entry_points.txt").write_text(f"[{group}]\n{lines}\n")


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    d = tmp_path / "site-packages"
    d.mkdir()
    monkeypatch.syspath_prepend(str(d))
    yield d
    for mod in [m for m in sys.modules if m.startswith(("tmp_file_notifier", "badplug"))]:
        del sys.modules[mod]


async def test_sample_file_notifier_works_without_core_changes(
    site: Path, cfg_dict: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_dist(
        site,
        "tell-my-pc-file-notifier",
        "tell_my_pc.notifiers",
        {"file": "tmp_file_notifier:create"},
        SAMPLE,
    )
    out = tmp_path / "notes.jsonl"
    cfg_dict["notifier"] = {"type": "file", "options": {"path": str(out)}}
    cfg = Config.model_validate(cfg_dict)
    notifier = build_notifier(cfg)
    assert notifier is not None and notifier.name == "file"

    store = Store(tmp_path / "data" / "state.db")
    d = Daemon(cfg, store, FakeExecutor([{"say": "Plugin says hi."}]), notifier, tmp_path / "data")
    await d.start()
    try:
        await d.submit(IncomingTask("check the plugin"))
        await d.wait_idle()
    finally:
        await d.close()
        store.close()
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["kind"] == "report" and "Plugin says hi." in records[-1]["message"]


async def test_also_notify_fans_out_but_not_approvals(
    site: Path, cfg_dict: dict, tmp_path: Path
) -> None:
    from tell_my_pc.plugins.api import Notification

    install_fake_dist(
        site, "tmpfn", "tell_my_pc.notifiers", {"file": "tmp_file_notifier:create"}, SAMPLE
    )
    out = tmp_path / "copy.jsonl"
    cfg_dict["also_notify"] = [{"type": "file", "options": {"path": str(out)}}]
    n = build_notifier(Config.model_validate(cfg_dict))
    assert n is not None
    await n.send(Notification("report", "r", kind="report"))
    with pytest.raises(RuntimeError):  # no main notifier for approvals: fail closed
        await n.send(Notification("approve?", "a", kind="approval"))
    kinds = [json.loads(x)["kind"] for x in out.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["report"]


def test_plugins_command_lists_it(site: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fake_dist(
        site, "tmpfn", "tell_my_pc.notifiers", {"file": "tmp_file_notifier:create"}, SAMPLE
    )
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert "file" in out and "tmpfn" in out and "ntfy" in out


def _bad_plugin(site: Path, body: str) -> None:
    pkg = site / "badplug"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent(body))
    install_fake_dist(site, "badplug", "tell_my_pc.notifiers", {"bad": "badplug:create"})


def test_incompatible_api_version_refused(site: Path, tmp_path: Path) -> None:
    _bad_plugin(
        site,
        """
        class N:
            api_version = "9.0"
            name = "bad"
            async def send(self, note): ...
        def create(options, ctx):
            return N()
        """,
    )
    with pytest.raises(registry.PluginError, match=r"plugin API 9.0"):
        registry.load("notifier", "bad", {}, tmp_path)


def test_wrong_shape_refused(site: Path, tmp_path: Path) -> None:
    _bad_plugin(
        site,
        """
        class N:
            api_version = "1.0"
            name = "bad"
        def create(options, ctx):
            return N()
        """,
    )
    with pytest.raises(registry.PluginError, match="does not implement"):
        registry.load("notifier", "bad", {}, tmp_path)


def test_plugin_cannot_shadow_builtin(site: Path) -> None:
    install_fake_dist(site, "evil", "tell_my_pc.notifiers", {"ntfy": "evil:create"})
    assert "ntfy" not in registry.discover("notifier")


def test_missing_plugin_named(tmp_path: Path) -> None:
    with pytest.raises(registry.PluginError, match="No notifier plugin named 'nope'"):
        registry.load("notifier", "nope", {}, tmp_path)


def test_builtin_names_rejected_as_plugin_config(cfg_dict: dict) -> None:
    cfg_dict["also_notify"] = [{"type": "ntfy", "options": {}}]
    with pytest.raises(Exception, match="topic"):
        Config.model_validate(cfg_dict)


def test_check_loads_plugins(site: Path, repo: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "server": {"bind": "127.0.0.1"},
                "projects": [{"name": "x", "path": str(repo)}],
                "notifier": {"type": "missing-plugin"},
                "executor": {"type": "fake"},
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(cfg), "check"]) == 1
