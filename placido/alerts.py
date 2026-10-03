"""Telling the user: a Herdr notification and, when the user turned it on, an email.

Decided with the user on 2026-10-03: everything that needs the user or ends a run
(each Herdr notification placido sends) is also emailed, in simple HTML, with both
subscriptions' quota. Email is off unless the user's own config
(~/.config/placido/config.toml) enables it; the Resend API key is read from a file at
send time and never copied. A failed email is logged and never stops a run.
"""

from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from placido import config, quota, runlog, status
from placido.herdr import Herdr, HerdrError

RESEND_URL = "https://api.resend.com/emails"
Sender = Callable[[str, dict[str, Any]], None]  # (api key, payload)


def _post(key: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        RESEND_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "placido"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise OSError(f"Resend answered {error.code}: {error.read().decode('utf-8', 'replace')[:300]}") from None


SENDER: list[Sender] = [_post]  # what email() sends with; the test suite replaces it


def notify(run: runlog.Run, herdr: Herdr, title: str, body: str) -> None:
    """A Herdr notification naming the issue, and the same as an email."""

    try:
        herdr.notify(f"placido · {run.path.parent.name}: {title}", body, "request")
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))
    email(run, title, body)


def email(run: runlog.Run, title: str, body: str, settings: config.UserConfig | None = None) -> None:
    try:
        settings = settings or config.load_user()
    except config.ConfigError as error:
        run.event("email.failed", error=str(error))
        return
    if not settings.email.enabled:
        return
    record = _record(run.path)
    issue = f"#{record['issue_number']}" if record.get("issue_number") else run.path.parent.name
    subject = f"placido · {record.get('project') or run.path.parent.parent.name} {issue}: {title}"
    try:
        key = Path(os.path.expanduser(settings.email.api_key_file)).read_text(encoding="utf-8").strip()
        if not key:
            raise OSError(f"{settings.email.api_key_file} is empty")
        SENDER[0](key, {
            "from": settings.email.sender, "to": [settings.email.to], "subject": subject,
            "html": render(run.path, title, body),
        })
    except OSError as error:
        run.event("email.failed", subject=subject, error=str(error)[:300])
        return
    run.event("email.sent", subject=subject, to=settings.email.to)


def _record(run_dir: Path) -> dict[str, Any]:
    try:
        return runlog.read_record(run_dir)
    except (OSError, ValueError):
        return {}


# ---- the message --------------------------------------------------------------------------


STYLE = "font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px;color:#1f2328;"
CELL = "padding:4px 12px 4px 0;vertical-align:top;"
LABEL = CELL + "color:#656d76;white-space:nowrap;"


def render(run_dir: Path, title: str, body: str, now: float | None = None) -> str:
    """The email's HTML: what happened, the run's facts, and the subscriptions' quota."""

    now = time.time() if now is None else now
    record = _record(run_dir)
    events = list(runlog.read_events(run_dir)) if (run_dir / "events.jsonl").is_file() else []
    opened = next((e for e in reversed(events) if e.get("event") == "pr.opened"), None)
    created = next((e for e in reversed(events) if e.get("event") == "worktree.created"), {})
    rows = [("Project", html.escape(str(record.get("project") or run_dir.parent.parent.name)))]
    issue = html.escape(str(record.get("issue_title") or run_dir.parent.name))
    if record.get("issue_source", "").startswith("http"):
        issue = f'<a href="{html.escape(record["issue_source"])}">{issue}</a>'
    rows.append(("Issue", issue))
    state = status.read(run_dir)
    if state:
        rows.append(("Status", html.escape(state)))
    if opened:
        url = html.escape(str(opened.get("url")))
        rows.append(("Pull request", f'<a href="{url}">{url}</a>'))
    if created.get("path"):
        rows.append(("Worktree", f"<code>{html.escape(str(created['path']))}</code>"))
    rows.append(("Run", f"<code>{html.escape(str(run_dir))}</code>"))
    table = "".join(f'<tr><td style="{LABEL}">{k}</td><td style="{CELL}">{v}</td></tr>' for k, v in rows)
    readings = [r for r in (quota.claude(), quota.codex()) if r is not None]
    quota_rows = "".join(
        f'<tr><td style="{LABEL}">{r.agent}</td><td style="{CELL}">{_windows(r, now)}</td></tr>' for r in readings
    ) or f'<tr><td style="{CELL}">No reading yet.</td></tr>'
    return (
        f'<div style="{STYLE}max-width:640px">'
        f'<h2 style="font-size:18px;margin:0 0 6px">{html.escape(title)}</h2>'
        f'<p style="margin:0 0 16px">{html.escape(body)}</p>'
        f'<table style="border-collapse:collapse;margin-bottom:16px">{table}</table>'
        f'<h3 style="font-size:15px;margin:0 0 4px">Quota</h3>'
        f'<table style="border-collapse:collapse;margin-bottom:16px">{quota_rows}</table>'
        f'<p style="color:#656d76;font-size:12px;margin:0">placido · '
        f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(now))}</p></div>'
    )


def _windows(reading: quota.Reading, now: float) -> str:
    parts = []
    for window in reading.windows:
        text = f"{window.label}: <b>{window.used:.0f}%</b> used"
        if window.resets_at:
            text += f", resets in {_span(window.resets_at - now)}"
        parts.append(text)
    age = f' <span style="color:#656d76">(as of {_span(now - reading.as_of)} ago)</span>' if reading.as_of else ""
    return " · ".join(parts) + age


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def test(settings: config.UserConfig | None = None, now: float | None = None) -> str:
    """Send a test email with the current quota; returns what happened, for `placido email-test`."""

    settings = settings or config.load_user()
    if not settings.email.enabled:
        return f"email is off; turn it on in {config.user_config_path()}"
    now = time.time() if now is None else now
    readings = [r for r in (quota.claude(), quota.codex()) if r is not None]
    rows = "".join(
        f'<tr><td style="{LABEL}">{r.agent}</td><td style="{CELL}">{_windows(r, now)}</td></tr>' for r in readings
    ) or f'<tr><td style="{CELL}">No reading yet.</td></tr>'
    page = (
        f'<div style="{STYLE}max-width:640px"><h2 style="font-size:18px;margin:0 0 6px">Placido can email you</h2>'
        f'<p style="margin:0 0 16px">This is a test. Runs will email you here when they need you or end.</p>'
        f'<h3 style="font-size:15px;margin:0 0 4px">Quota</h3>'
        f'<table style="border-collapse:collapse">{rows}</table></div>'
    )
    try:
        key = Path(os.path.expanduser(settings.email.api_key_file)).read_text(encoding="utf-8").strip()
        SENDER[0](key, {"from": settings.email.sender, "to": [settings.email.to],
                        "subject": "placido · test email", "html": page})
    except OSError as error:
        return f"failed: {error}"
    return f"sent to {settings.email.to}"
