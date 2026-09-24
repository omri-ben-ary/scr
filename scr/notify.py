"""Out-of-band push of needs-you cards (T-0090): Omri learns the system is stuck without opening the page.

The watcher calls `push()` once per tick with the cards the page shows (scr/ui.py needs_cards, one source).
A card is pushed once: its key (the one `scr ack` takes: `events:<task>:<kind>:<ts>`, `chore:<id>`,
`decision:<id>`, ...) becomes a `notified:<key>` line in the watcher flags file (.scr/watch.state.txt), which
survives a restart; an acked key (reports/ui.acks.txt) is never sent. A `notified:` line whose card no longer
stands is dropped, so the flags file stays as long as the card list.

Channels, each on/off in config/notify.yaml (absent file = all off):
  toast  a Windows toast through `powershell -Command` and the WinRT ToastNotificationManager (no module)
  email  SMTP; host/port/from/to/user in the yaml, the password in the environment variable the yaml names
A channel is `fn(cards, channel_cfg, cfg) -> str`; it raises on failure, and `send` turns that into a
`notify FAILED: <channel>: ...` line, never an exception.
"""
from __future__ import annotations

import os
import shutil
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

import yaml

FLAG_PREFIX = "notified:"
# the AppUserModelID of Windows PowerShell: registered on every Windows 10/11, so an unpackaged script may toast under it
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
DEFAULT_URL = "http://127.0.0.1:8765/"


def load(root: Path) -> dict:
    p = root / "config" / "notify.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def enabled(cfg: dict) -> list[str]:
    return [n for n in CHANNELS if isinstance(cfg.get(n), dict) and cfg[n].get("enabled")]


def _line(c: dict) -> str:
    return " ".join(f"{c.get('id') or ''} {c.get('title') or c.get('kind') or ''}".split())


def _under_pytest() -> bool:
    # the test fixtures copy the live config/ into every test world: a real toast or mail from a test is never wanted
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


# ---- toast ---------------------------------------------------------------------------------------------
def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def toast_text(cards: list[dict]) -> tuple[str, str]:
    head = f"Scriptorium needs you: {len(cards)} new card" + ("s" if len(cards) != 1 else "")
    body = " | ".join(_line(c) for c in cards[:4]) + (f" | and {len(cards) - 4} more" if len(cards) > 4 else "")
    return head, body[:600]


def toast_argv(cards: list[dict], exe: str = "powershell") -> list[str]:
    """The command that shows one toast for `cards`: head line + one `<id> <title>` entry per card."""
    head, body = toast_text(cards)
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{escape(head)}</text><text>{escape(body)}</text>"
           "</binding></visual></toast>")
    script = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
              "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
              "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; "
              f"$x.LoadXml({_ps_quote(xml)}); "
              f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(TOAST_APP_ID)})"
              ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def toast(cards: list[dict], ch: dict, cfg: dict) -> str:
    if _under_pytest():
        return "skipped (pytest)"
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise RuntimeError("no powershell on PATH: toast unavailable")
    r = subprocess.run(toast_argv(cards, exe), capture_output=True, text=True, timeout=int(ch.get("timeout", 30)),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"powershell rc {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return f"shown ({len(cards)} card(s))"


# ---- email ---------------------------------------------------------------------------------------------
def email_message(cards: list[dict], ch: dict, cfg: dict) -> EmailMessage:
    to = ch.get("to") or []
    to = [to] if isinstance(to, str) else list(to)
    if not ch.get("host") or not ch.get("from") or not to:
        raise ValueError("email needs host, from and to in config/notify.yaml")
    m = EmailMessage()
    m["Subject"] = f"[Scriptorium] {len(cards)} new card" + ("s" if len(cards) != 1 else "") + " need you"
    m["From"], m["To"] = ch["from"], ", ".join(to)
    parts = [f"Open {cfg.get('ui_url') or DEFAULT_URL}", ""]
    for c in cards:
        parts += [f"- {_line(c)}  [{c.get('type')}]  key {c.get('key')}"]
        if c.get("text"):
            parts += ["    " + ln for ln in str(c["text"]).splitlines()[:8]]
    parts += ["", "Silence a card: scr ack <key>, or Dismiss on the page."]
    m.set_content("\n".join(parts))
    return m


def email(cards: list[dict], ch: dict, cfg: dict) -> str:
    msg = email_message(cards, ch, cfg)
    if _under_pytest():
        return "skipped (pytest)"
    password = None
    if ch.get("password_env"):
        password = os.environ.get(ch["password_env"])
        if password is None:
            raise RuntimeError(f"environment variable {ch['password_env']} (password_env) is not set")
    port, timeout = int(ch.get("port", 587)), int(ch.get("timeout", 30))
    cls = smtplib.SMTP_SSL if ch.get("ssl") else smtplib.SMTP
    with cls(ch["host"], port, timeout=timeout) as s:
        if not ch.get("ssl") and ch.get("starttls", True):
            s.starttls()
        if ch.get("user"):
            s.login(ch["user"], password or "")
        s.send_message(msg)
    return f"sent to {msg['To']}"


CHANNELS: dict[str, Callable[[list[dict], dict, dict], str]] = {"toast": toast, "email": email}


# ---- dispatch --------------------------------------------------------------------------------------------
def send(cfg: dict, cards: list[dict], names: list[str] | None = None) -> list[str]:
    """One call per channel with every card; one result line per channel. Never raises."""
    out = []
    for name in (enabled(cfg) if names is None else names):
        try:
            out.append(f"notify {name}: {CHANNELS[name](cards, cfg.get(name) or {}, cfg)}")
        except Exception as e:  # noqa: BLE001 — a notification failure is a log line, never a tick crash
            out.append(f"notify FAILED: {name}: {type(e).__name__}: {e}")
    return out


def pending(cards: list[dict], flags: set[str], acked: set[str]) -> list[dict]:
    """The cards not yet pushed and not acked."""
    return [c for c in cards if c.get("key") and c["key"] not in acked and FLAG_PREFIX + c["key"] not in flags]


def push(root: Path, cards: list[dict], flags: set[str], acked: set[str], log: Callable[[str], None]) -> list[dict]:
    """Push the NEW cards on every enabled channel; record them in `flags` (the watcher saves it). Returns them.
    With no channel enabled nothing is sent or recorded, so turning a channel on pushes what stands then."""
    live = {FLAG_PREFIX + c["key"] for c in cards if c.get("key")}
    for k in [k for k in flags if k.startswith(FLAG_PREFIX) and k not in live]:
        flags.discard(k)
    cfg = load(root)
    names = enabled(cfg)
    if not names:
        return []
    new = pending(cards, flags, acked)
    if not new:
        return []
    flags.update(FLAG_PREFIX + c["key"] for c in new)     # once per card, even if a channel fails (no retry storm)
    log(f"notify: {len(new)} new card(s): " + ", ".join(c["key"] for c in new)[:400])
    for line in send(cfg, new, names):
        log(line)
    return new


def send_test(root: Path) -> list[str]:
    """`scr notify --test`: one test card on every enabled channel; one result line per channel."""
    cfg = load(root)
    if not (root / "config" / "notify.yaml").exists():
        return ["config/notify.yaml absent: every channel is off"]
    card = {"key": "test", "id": "TEST", "type": "test", "title": "scr notify --test: the push channel works",
            "text": "A test message from scr notify --test. Nothing needs you."}
    lines = [f"notify {n}: off" for n in CHANNELS if n not in enabled(cfg)]
    return lines + send(cfg, [card])
