"""CI: write the real autostart file for this OS so the native tool can lint it.

Usage: python scripts/ci_autostart_files.py OUT_DIR   (prints the written path)
"""

import sys
from pathlib import Path

from tell_my_pc import autostart

out = Path(sys.argv[1])
cfg = out / "it's 100% $HOME" / "config.yaml"  # needs escaping in every format
cfg.parent.mkdir(parents=True, exist_ok=True)
p = autostart.plan("install", cfg, home=out / "home", has=lambda tool: True)
for path, text in p.writes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)
