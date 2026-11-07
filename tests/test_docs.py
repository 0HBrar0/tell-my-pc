"""The docs folder is published as a site: its relative links must stay inside it and resolve."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parent.parent / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")


def relative_links(md: Path) -> list[str]:
    text = re.sub(r"```.*?```", "", md.read_text(encoding="utf-8"), flags=re.S)
    return [
        m.split("#")[0]
        for m in LINK.findall(text)
        if not re.match(r"[a-z]+:", m) and not m.startswith("#")
    ]


def test_docs_links_resolve_inside_docs() -> None:
    broken = []
    for md in DOCS.rglob("*.md"):
        for link in relative_links(md):
            target = (md.parent / link).resolve()
            if DOCS not in target.parents and target != DOCS:
                broken.append(f"{md.relative_to(DOCS)}: {link} leaves docs/ (use a GitHub URL)")
            elif not target.exists():
                broken.append(f"{md.relative_to(DOCS)}: {link} does not exist")
            elif target.is_dir() and not (target / "index.md").exists():
                broken.append(f"{md.relative_to(DOCS)}: {link} is a folder without index.md")
    assert broken == []


def test_site_config_parses() -> None:
    cfg = yaml.safe_load((DOCS / "_config.yml").read_text(encoding="utf-8"))
    assert cfg["title"] == "tell-my-pc" and "theme" in cfg
    assert (DOCS / "index.md").exists()


def test_every_adr_is_listed() -> None:
    index = (DOCS / "decisions" / "index.md").read_text(encoding="utf-8")
    for adr in (DOCS / "decisions").glob("0*.md"):
        assert adr.name in index, adr.name
