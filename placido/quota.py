"""How much of each subscription is used, for the email alerts.

Codex records its rate limits in every session file (`token_count` events), so the
newest session tells the current reading. Claude Code records its quota nowhere on
disk; it hands it only to the status line, whose script saves it, with the time, to
~/placido/claude-quota.json (set up with the user on 2026-10-03).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CLAUDE_FILE = "~/placido/claude-quota.json"
CODEX_SESSIONS = "~/.codex/sessions"
TAIL_BYTES = 400_000  # a session's last token_count is near its end


@dataclass(frozen=True)
class Window:
    label: str  # "5h", "7d"
    used: float  # percent
    resets_at: float | None  # epoch seconds


@dataclass(frozen=True)
class Reading:
    agent: str
    windows: tuple[Window, ...]
    as_of: float | None  # epoch seconds of the reading


def claude(path: str | Path = CLAUDE_FILE) -> Reading | None:
    try:
        data = json.loads(Path(os.path.expanduser(str(path))).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    limits = data.get("rate_limits") or {}
    windows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = limits.get(key)
        if isinstance(window, dict) and window.get("used_percentage") is not None:
            windows.append(Window(label, float(window["used_percentage"]), _number(window.get("resets_at"))))
    return Reading("claude", tuple(windows), _number(data.get("saved_at"))) if windows else None


def codex(root: str | Path = CODEX_SESSIONS) -> Reading | None:
    """The rate limits of the newest codex session that recorded any."""

    base = Path(os.path.expanduser(str(root)))
    days = sorted((d for d in base.glob("*/*/*") if d.is_dir()), reverse=True)[:7]
    files = sorted((f for d in days for f in d.glob("*.jsonl")), key=lambda f: f.stat().st_mtime, reverse=True)
    for file in files[:20]:
        found = _last_limits(file)
        if found is not None:
            limits, stamp = found
            windows = tuple(
                Window(_label(w.get("window_minutes")), float(w["used_percent"]), _number(w.get("resets_at")))
                for w in (limits.get("primary"), limits.get("secondary"))
                if isinstance(w, dict) and w.get("used_percent") is not None
            )
            if windows:
                return Reading("codex", windows, stamp or file.stat().st_mtime)
    return None


def _last_limits(file: Path) -> tuple[dict[str, Any], float | None] | None:
    try:
        with file.open("rb") as handle:
            handle.seek(max(0, file.stat().st_size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = (event.get("payload") or {}).get("rate_limits")
        if isinstance(limits, dict):
            return limits, _timestamp(event.get("timestamp"))
    return None


def _label(minutes: Any) -> str:
    if not minutes:
        return "window"
    minutes = int(minutes)
    return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
