"""Generate ``config/schema.json`` from the config models."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import Config


def schema_text() -> str:
    schema = Config.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "tell-my-pc config"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[0]) if argv else Path("config/schema.json")
    out.write_text(schema_text(), encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
