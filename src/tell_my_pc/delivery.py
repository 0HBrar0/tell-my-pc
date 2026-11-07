"""Sending files to the phone: path checks, signed expiring links (Phase 4).

A task asks for a file through the ``deliver_file`` tool (see :mod:`tell_my_pc.mcp_server`).
The file must sit inside the task's project or a configured ``delivery.roots`` folder, must
not match a Never rule, and must be under the size limit. It is recorded in the state DB; the
daemon then sends it as an ntfy attachment (small files) or as a link like
``<public_url>/files/<id>?exp=<epoch>&sig=<hmac>`` that stops working after ``exp``.

The HMAC key lives in the data dir (a Never path for tasks), never in config.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .plugins.api import PLUGIN_API_VERSION
from .policy import PolicySpec, _PathCheck, within

KEY_FILE = "delivery.key"


class DeliveryRefused(Exception):
    pass


def load_key(data_dir: Path) -> bytes:
    """The link-signing key, created on first use with owner-only permissions."""
    path = data_dir / KEY_FILE
    if path.exists():
        key = path.read_bytes()
        if len(key) >= 32:
            return key
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def sign(key: bytes, delivery_id: str, exp: int) -> str:
    msg = f"{delivery_id}.{exp}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:40]


def verify(key: bytes, delivery_id: str, exp: str, sig: str, now: float | None = None) -> bool:
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if exp_i < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sign(key, delivery_id, exp_i), sig)


def link(base_url: str, key: bytes, delivery_id: str, exp: float) -> str:
    e = int(exp)
    return f"{base_url.rstrip('/')}/files/{delivery_id}?exp={e}&sig={sign(key, delivery_id, e)}"


@dataclass
class CheckedFile:
    path: Path
    size: int


def check_file(spec: PolicySpec, raw: str) -> CheckedFile:
    """Raise DeliveryRefused unless ``raw`` may be sent to the phone."""
    if not raw or not raw.strip():
        raise DeliveryRefused("no path given")
    checker = _PathCheck(spec)
    hit = checker.never(raw)
    if hit:
        raise DeliveryRefused(f"Never rule: {raw} matches {hit}")
    lexical, resolved = checker.forms(raw)
    roots = [r for root in [spec.project_dir, *spec.delivery_roots] for r in checker.forms(root)]
    if not (any(within(lexical, r) for r in roots) and any(within(resolved, r) for r in roots)):
        raise DeliveryRefused(
            f"{raw} is outside the project and the allowed delivery folders "
            "(delivery.roots in the config)"
        )
    path = Path(os.path.realpath(_native(raw, spec)))
    if not path.is_file():
        raise DeliveryRefused(f"{raw} is not a file")
    size = path.stat().st_size
    if size > spec.delivery_max_bytes:
        mb = spec.delivery_max_bytes / 1_000_000
        raise DeliveryRefused(f"{raw} is larger than the {mb:.0f} MB delivery limit")
    return CheckedFile(path, size)


def _native(raw: str, spec: PolicySpec) -> str:
    from .policy import _expand

    p = _expand(raw, spec.home)
    return p if os.path.isabs(p) else os.path.join(spec.project_dir, p)


class SignedLinkDelivery:
    """The built-in :class:`~tell_my_pc.plugins.api.Delivery`: signed, expiring links."""

    api_version: ClassVar[str] = PLUGIN_API_VERSION
    name: ClassVar[str] = "signed_link"

    def __init__(
        self, base_url: str, key: bytes, register: Callable[[str, int], tuple[str, float]]
    ) -> None:
        self.base_url = base_url
        self.key = key
        self._register = register

    def link_for(self, path: str, ttl_s: int) -> str:
        did, exp = self._register(path, ttl_s)
        return link(self.base_url, self.key, did, exp)
