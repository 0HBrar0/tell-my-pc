"""Map a spoken project name to a configured project."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .config import ProjectConfig

_FILLER = re.compile(r"\b(the|my|project|repo|repository|folder)\b", re.I)


def _key(s: str) -> str:
    s = _FILLER.sub(" ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


@dataclass
class Resolution:
    project: ProjectConfig | None
    candidates: list[ProjectConfig]
    reason: str


def resolve(projects: list[ProjectConfig], name: str | None, text: str = "") -> Resolution:
    names: dict[str, ProjectConfig] = {}
    for p in projects:
        for n in (p.name, *p.aliases):
            k = _key(n)
            if k:
                names.setdefault(k, p)

    if name and name.strip():
        k = _key(name)
        if k in names:
            return Resolution(names[k], [], "exact")
        close = difflib.get_close_matches(k, list(names), n=3, cutoff=0.72)
        found = _unique([names[c] for c in close])
        if len(found) == 1:
            return Resolution(found[0], [], f"fuzzy match for {name!r}")
        return Resolution(None, found or list(projects), f"not sure which project {name!r} is")

    # No explicit name: look for a mention in the task text.
    t = f" {_key(text)} "
    mentioned = _unique([p for k, p in names.items() if f" {k} " in t])
    if len(mentioned) == 1:
        return Resolution(mentioned[0], [], "mentioned in the task")
    if len(projects) == 1:
        return Resolution(projects[0], [], "only project")
    return Resolution(None, mentioned or list(projects), "no project named")


def _unique(items: list[ProjectConfig]) -> list[ProjectConfig]:
    seen: list[ProjectConfig] = []
    for p in items:
        if all(p.name != s.name for s in seen):
            seen.append(p)
    return seen
