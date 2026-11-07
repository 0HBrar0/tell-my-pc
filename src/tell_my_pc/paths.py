"""Where config and state live. Never inside the repo."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP = "tell-my-pc"


def config_dir() -> Path:
    override = os.environ.get("TELL_MY_PC_CONFIG_DIR")
    return Path(override) if override else Path(user_config_dir(APP, appauthor=False))


def data_dir() -> Path:
    override = os.environ.get("TELL_MY_PC_DATA_DIR")
    return Path(override) if override else Path(user_data_dir(APP, appauthor=False))


def config_file() -> Path:
    return config_dir() / "config.yaml"
