"""Find and load plugins through Python entry points.

A plugin package declares, in its ``pyproject.toml``::

    [project.entry-points."tell_my_pc.notifiers"]
    file = "my_package:create"

``create(options, ctx)`` returns an object implementing the matching protocol from
:mod:`tell_my_pc.plugins.api`, with ``api_version`` and ``name`` class attributes. The
registry refuses plugins whose ``api_version`` is not compatible with
:data:`~tell_my_pc.plugins.api.PLUGIN_API_VERSION`, and plugins that try to shadow a
built-in name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Literal

from .api import PLUGIN_API_VERSION, Delivery, Executor, Notifier, Transport, api_compatible

log = logging.getLogger("tell_my_pc.plugins")

Kind = Literal["notifier", "executor", "transport", "delivery"]

GROUPS: dict[Kind, str] = {
    "notifier": "tell_my_pc.notifiers",
    "executor": "tell_my_pc.executors",
    "transport": "tell_my_pc.transports",
    "delivery": "tell_my_pc.delivery",
}

PROTOCOLS: dict[Kind, type] = {
    "notifier": Notifier,
    "executor": Executor,
    "transport": Transport,
    "delivery": Delivery,
}

BUILTINS: dict[Kind, frozenset[str]] = {
    "notifier": frozenset({"ntfy"}),
    "executor": frozenset({"claude_code", "fake"}),
    "transport": frozenset({"tailscale_http", "ntfy_inbox"}),
    "delivery": frozenset(),
}


class PluginError(Exception):
    pass


@dataclass
class PluginContext:
    """What the daemon hands a plugin factory. Plugins must not read the config file or
    anything else of tell-my-pc's on their own."""

    kind: Kind
    name: str
    data_dir: Path
    """A private folder for this plugin: ``<data dir>/plugins/<kind>/<name>``."""
    api_version: str = PLUGIN_API_VERSION
    logger: logging.Logger = field(default_factory=lambda: log)


@dataclass
class PluginInfo:
    kind: Kind
    name: str
    target: str
    distribution: str | None
    builtin: bool = False


def discover(kind: Kind) -> dict[str, EntryPoint]:
    found: dict[str, EntryPoint] = {}
    for ep in entry_points(group=GROUPS[kind]):
        if ep.name in BUILTINS[kind]:
            log.warning("ignoring plugin %r: it would shadow a built-in %s", ep.name, kind)
            continue
        if ep.name in found:
            log.warning("two plugins named %r for %s; using the first", ep.name, kind)
            continue
        found[ep.name] = ep
    return found


def list_plugins() -> list[PluginInfo]:
    out: list[PluginInfo] = []
    for kind in GROUPS:
        out += [PluginInfo(kind, n, "built in", None, True) for n in sorted(BUILTINS[kind])]
        for name, ep in sorted(discover(kind).items()):
            dist = ep.dist.name if ep.dist else None
            out.append(PluginInfo(kind, name, ep.value, dist))
    return out


def load(kind: Kind, name: str, options: dict[str, Any], data_dir: Path) -> Any:
    eps = discover(kind)
    if name not in eps:
        known = ", ".join(sorted(BUILTINS[kind] | set(eps))) or "none"
        raise PluginError(f"No {kind} plugin named {name!r} is installed (known: {known}).")
    try:
        factory = eps[name].load()
    except Exception as e:
        raise PluginError(f"Could not import {kind} plugin {name!r}: {e}") from e
    pdir = data_dir / "plugins" / kind / name
    pdir.mkdir(parents=True, exist_ok=True)
    ctx = PluginContext(kind=kind, name=name, data_dir=pdir)
    try:
        obj = factory(dict(options), ctx)
    except Exception as e:
        raise PluginError(f"{kind} plugin {name!r} failed to start: {e}") from e
    check(kind, name, obj)
    return obj


def check(kind: Kind, name: str, obj: Any) -> None:
    version = getattr(obj, "api_version", None)
    if not isinstance(version, str):
        raise PluginError(f"{kind} plugin {name!r} does not declare api_version.")
    try:
        ok = api_compatible(version)
    except ValueError:
        ok = False
    if not ok:
        raise PluginError(
            f"{kind} plugin {name!r} targets plugin API {version}; this tell-my-pc speaks "
            f"{PLUGIN_API_VERSION}. Upgrade the plugin or tell-my-pc."
        )
    if not isinstance(obj, PROTOCOLS[kind]):
        raise PluginError(f"{kind} plugin {name!r} does not implement the {kind} interface.")
