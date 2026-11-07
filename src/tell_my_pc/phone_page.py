"""A private phone-setup page with QR codes, so nothing long has to be typed on the iPhone.

The page contains the bearer token: it is written to the data dir and never published.
"""

from __future__ import annotations

import html
from pathlib import Path

import segno

from .config import Config, NtfyConfig

CSS = """
body{font:16px/1.5 system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;
color:#111;background:#fafafa}
h1{font-size:26px} h2{margin-top:32px;font-size:20px} h3{font-size:16px}
.grid{display:flex;flex-wrap:wrap;gap:16px}
.card{background:#fff;border:1px solid #ddd;border-radius:12px;padding:16px;width:280px}
.card h3{margin:0 0 8px}
code{word-break:break-all;font-size:13px;background:#f1f1f1;padding:2px 5px;border-radius:5px}
.card code{display:block;font-size:12px;padding:6px;margin-top:8px}
ol li{margin:6px 0} .warn{background:#fff4d6;padding:10px 14px;border-radius:8px}
"""


def _qr(text: str) -> str:
    return segno.make(text, error="m").svg_inline(scale=5, dark="#111", light="#fff")


def _card(title: str, value: str, note: str) -> str:
    return (
        f'<div class="card"><h3>{html.escape(title)}</h3>{_qr(value)}'
        f"<code>{html.escape(value)}</code><p>{html.escape(note)}</p></div>"
    )


def _c(text: str) -> str:
    return f"<code>{html.escape(text)}</code>"


def render(cfg: Config) -> str:
    base = (cfg.server.public_url or "").rstrip("/") or "http://<your PC's Tailscale IP>:8787"
    token = cfg.server.token.resolve() or ""
    topic = cfg.notifier.topic if isinstance(cfg.notifier, NtfyConfig) else None
    server = cfg.notifier.server if isinstance(cfg.notifier, NtfyConfig) else ""
    projects = ", ".join(p.name for p in cfg.projects)
    cards = []
    if topic:
        cards.append(
            _card("1. ntfy topic", topic, f"Subscribe to this in the ntfy app ({server}).")
        )
    cards.append(_card("2. Task URL", base + "/task", "For the Shortcut's Get Contents of URL."))
    cards.append(
        _card("3. Authorization value", "Bearer " + token, "The Authorization header value.")
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tell-my-pc phone setup</title><style>{CSS}</style></head><body>
<h1>tell-my-pc: phone setup</h1>
<p class="warn">This page contains your private token. Don't share it. Point the iPhone camera
at a QR code, tap the banner, then copy the text.</p>
<div class="grid">{"".join(cards)}</div>

<h2>A. Notifications (2 min)</h2>
<ol>
<li>Install <b>ntfy</b> from the App Store, open it, allow notifications.</li>
<li>Tap <b>+</b>, paste the topic from QR 1, keep the server as shown, tap Subscribe.</li>
</ol>

<h2>B. Tailscale (1 min)</h2>
<ol>
<li>Install <b>Tailscale</b> from the App Store and sign in with the same account as this
computer.</li>
<li>Turn the main switch <b>on</b> and leave it on. While it's on, Tailscale keeps itself
connected across restarts and Wi-Fi/mobile changes.</li>
<li>Check: open {_c(base + "/health")} in Safari. You should see {_c('{"ok":true}')}. If it
keeps loading, allow the port in the Windows firewall (the setup wizard printed the
command).</li>
</ol>

<h2>C. The "Tell Claude" Shortcut (5 min)</h2>
<ol>
<li>Shortcuts app → <b>+</b>. Rename it <b>Tell Claude</b> (that's the Siri phrase).</li>
<li>Add <b>Dictate Text</b> (Stop Listening: After Pause).</li>
<li>Add <b>Current Date</b>.</li>
<li>Add <b>Get Time Between Dates</b>: from a date you type as
{_c("1 January 1970 00:00 UTC")} to <b>Current Date</b>, in <b>Seconds</b>.</li>
<li>Add <b>Random Number</b> between 100000000 and 999999999999.</li>
<li>Add <b>Get Contents of URL</b>: URL from QR 2, Method <b>POST</b>. Headers:
{_c("Authorization")} = text from QR 3, {_c("X-TMP-Timestamp")} = Time Between Dates,
{_c("X-TMP-Nonce")} = Random Number. Request Body <b>JSON</b> with field {_c("text")} =
Dictated Text.</li>
<li>Add <b>Get Dictionary Value</b>: key {_c("speak")} from Contents of URL.</li>
<li>Add <b>Speak Text</b>: Dictionary Value. Done.</li>
</ol>
<p>Try: <i>"Hey Siri, Tell Claude"</i>, then a task such as "what still needs doing in
{html.escape(cfg.projects[0].name if cfg.projects else "my project")}". If Siri asks which
project, say "Tell Claude" again with just the name. Projects: {html.escape(projects)}.</p>
</body></html>
"""


def write(cfg: Config, data_dir: Path) -> Path:
    out = data_dir / "phone-setup.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(cfg), encoding="utf-8")
    return out
