from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tell_my_pc import config as C
from tell_my_pc.schema import schema_text

ROOT = Path(__file__).resolve().parents[1]


def make(d: dict) -> C.Config:
    return C.Config.model_validate(d)


def test_valid_config_has_no_problems(cfg_dict: dict) -> None:
    assert C.safety_problems(make(cfg_dict)) == []


ALL_INTERFACES = "0.0.0." + "0"  # built at runtime: refusal under test, not a real bind


@pytest.mark.parametrize("bind", [ALL_INTERFACES, "::", "192.168.1.10", "8.8.8.8", "not-an-ip"])
def test_refuses_non_tailscale_binds(cfg_dict: dict, bind: str) -> None:
    cfg_dict["server"]["bind"] = bind
    assert any("bind" in p for p in C.safety_problems(make(cfg_dict)))
    with pytest.raises(C.ConfigError):
        C.resolve_bind(make(cfg_dict))


@pytest.mark.parametrize("bind", ["127.0.0.1", "100.101.102.103", "fd7a:115c:a1e0::1"])
def test_accepts_tailscale_and_loopback(cfg_dict: dict, bind: str) -> None:
    cfg_dict["server"]["bind"] = bind
    assert C.check_bind(bind) == []
    assert C.resolve_bind(make(cfg_dict)) == bind


def test_refuses_missing_or_short_token(cfg_dict: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELL_MY_PC_TOKEN")
    assert any("token" in p.lower() for p in C.safety_problems(make(cfg_dict)))
    monkeypatch.setenv("TELL_MY_PC_TOKEN", "short")
    assert any("shorter" in p for p in C.safety_problems(make(cfg_dict)))


def test_refuses_empty_deny_list(cfg_dict: dict) -> None:
    cfg_dict["policy"] = {"builtin_never": False}
    assert any("Never deny list is empty" in p for p in C.safety_problems(make(cfg_dict)))
    cfg_dict["policy"] = {"builtin_never": False, "never_paths": ["~/secret/**"]}
    assert C.safety_problems(make(cfg_dict)) == []


def test_refuses_never_program_in_allowlist(cfg_dict: dict) -> None:
    cfg_dict["policy"] = {"work_commands": ["pytest", "powershell -c x", "reg.exe add"]}
    problems = C.safety_problems(make(cfg_dict))
    assert sum("Never program" in p for p in problems) == 2


def test_refuses_guessable_topic_and_plain_http(cfg_dict: dict) -> None:
    cfg_dict["notifier"] = {"server": "http://ntfy.example.com", "topic": "short"}
    problems = C.safety_problems(make(cfg_dict))
    assert any("https" in p for p in problems)
    assert any("guess" in p for p in problems)


def test_warns_without_ntfy_token(cfg_dict: dict) -> None:
    cfg_dict["notifier"] = {"topic": "a-long-random-topic-name"}
    assert any("access token" in w for w in C.safety_warnings(make(cfg_dict)))


def test_missing_project_dir(cfg_dict: dict, tmp_path: Path) -> None:
    cfg_dict["projects"][0]["path"] = str(tmp_path / "nope")
    assert any("does not exist" in p for p in C.safety_problems(make(cfg_dict)))


def test_unknown_keys_rejected(cfg_dict: dict) -> None:
    cfg_dict["server"]["typo"] = 1
    with pytest.raises(Exception, match="typo"):
        make(cfg_dict)


def test_example_config_parses() -> None:
    raw = yaml.safe_load((ROOT / "config" / "example.yaml").read_text(encoding="utf-8"))
    cfg = make(raw)
    assert cfg.server.bind == "tailscale"
    assert cfg.policy.builtin_never


def test_schema_file_is_current() -> None:
    current = (ROOT / "config" / "schema.json").read_text(encoding="utf-8")
    assert current == schema_text(), "run: uv run python -m tell_my_pc.schema"


def test_load_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(C.ConfigError, match="init"):
        C.load(tmp_path / "missing.yaml")
