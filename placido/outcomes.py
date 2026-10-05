"""Why an agent's last turn failed, read from its own session file.

Each agent records a failed turn its own way: codex copies the turn's error into
its `task_complete` event with a category, claude writes an "API Error" assistant
entry with an error type, and pi ends the turn's message with `stopReason: "error"`
and the provider's text. Categories are trusted where an agent gives one; the text
decides the rest. The screen is a backup for when no transcript can be found.

Only the last turn counts: an error the agent recovered from is history.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("refusal", "quota", "unavailable", "transient", "auth", "context", "error")


@dataclass(frozen=True)
class Failure:
    kind: str  # one of KINDS
    message: str
    resets_at: float | None = None  # epoch seconds when a quota frees up, if known


# ---- text --------------------------------------------------------------------------------

# Checked in this order: a message naming a limit and a 429 is a quota, not a blip.
TEXT_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # The model cannot run for this login at all, as codex's gpt-daybreak-blue-latest
    # once a ChatGPT account no longer could (2026-10-05): the next agent may.
    ("unavailable", (
        "is not supported when using", "model is not supported", "model_not_found", "model not found",
        "does not exist or you do not have access", "unknown model", "invalid model",
        "not available on your plan", "you do not have access to the model",
    )),
    ("auth", (
        "/login", "log in to", "log into", "sign in again", "not authenticated", "not logged in",
        "no api key", "no credential", "unauthorized", "invalid api key", "invalid auth token",
        "authentication failed", "failed to authenticate", "login expired", "token has expired",
        "could not be refreshed",
    )),
    ("quota", (
        "usage limit", "session limit", "weekly limit", "spend limit", "spend cap", "you've hit your",
        "you’ve hit your", "out of usage credits", "out of credits", "out of credit",
        "insufficient credits", "insufficient_quota", "credit balance", "purchase more credits",
        "credits depleted", "quota exceeded",
    )),
    ("refusal", (
        "flagged for possible", "safeguards flagged", "can't help with this", "can’t help with this",
        "content can't be shown", "content can’t be shown", "cybersecurity risk", "content policy",
        "usage policy", "trusted access", "safety system", "moderation", "content_filter",
        "misalignment policy", "limited access to this content for safety", "model refused",
        "refused to complete",
    )),
    ("context", (
        "prompt is too long", "context window", "context_length_exceeded", "context length exceeded",
        "maximum context length",
    )),
    ("transient", (
        "overloaded", "at capacity", "high demand", "high load", "temporarily", "econnreset",
        "connection error", "connection failed", "connection to the api was lost", "service unavailable",
        "internal server error", "stream disconnected", "timed out", "too many requests",
        "retry limit", "terminated", "fetch failed", "server-side issue",
    )),
)
TRANSIENT_STATUS = re.compile(r"\b(?:429|50[0234]|52[0-9])\b")


def classify(message: str) -> str:
    lower = message.lower()
    for kind, marks in TEXT_SIGNALS:
        if any(mark in lower for mark in marks):
            return kind
    if re.search(r"\b40[13]\b", lower):
        return "auth"
    if re.search(r"\b402\b", lower):
        return "quota"
    if TRANSIENT_STATUS.search(lower):
        return "transient"
    return "error"


# ---- reset times -------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
DATE = r"(?:([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,?(?: (\d{4}))?,? )?"


def parse_reset(message: str, now: datetime) -> float | None:
    """The epoch time a quota message says it resets, or None.

    Understands codex's "try again at 3:41 PM" or "try again at Oct 5th, 2026 3:41 PM"
    (local time), claude's "resets 3pm (Europe/Rome)" or "resets Oct 5, 3pm (tz)", and
    pi's "Try again in ~25 min". `now` must be timezone-aware."""

    relative = re.search(r"try again in ~?\s*(\d+)\s*(min|minute|h|hour)", message, re.I)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        return (now + (timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount))).timestamp()
    found = re.search(r"(?:try again at|resets(?: at)?) " + DATE + CLOCK + r"(?:\s*\(([^)]+)\))?", message, re.I)
    if not found:
        return None
    month, day, year, hour, minute, ampm, zone = found.groups()
    tz = now.tzinfo
    if zone:
        try:
            tz = ZoneInfo(zone.strip())
        except (ZoneInfoNotFoundError, ValueError):
            pass
    local_now = now.astimezone(tz)
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    when = local_now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    if month and month[:3].lower() in MONTHS:
        when = when.replace(year=int(year) if year else local_now.year, month=MONTHS[month[:3].lower()], day=int(day))
        if not year and when < local_now - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    elif when <= local_now:
        when += timedelta(days=1)  # a clock time already past today means tomorrow
    return when.timestamp()


# ---- transcripts -------------------------------------------------------------------------


def _lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def last_failure(transcript: Path, agent: str, now: datetime | None = None) -> Failure | None:
    """The failure that ended the agent's last turn, or None if it did not fail."""

    now = now or datetime.now().astimezone()
    reader = {"codex": _codex, "claude": _claude, "pi": _pi}.get(agent)
    return reader(list(_lines(transcript)), now) if reader else None


CODEX_KINDS = {
    "cyber_policy": "refusal",
    "bio_policy": "refusal",
    "misalignment_policy_violation": "refusal",
    "invalid_prompt": "refusal",
    "usage_limit_exceeded": "quota",
    "context_window_exceeded": "context",
    "unauthorized": "auth",
    "server_overloaded": "transient",
    "internal_server_error": "transient",
    "rate_limit_exceeded": "transient",
    "response_too_many_failed_attempts": "transient",
    "response_stream_connection_failed": "transient",
    "response_stream_disconnected": "transient",
}


def _codex(events: list[dict[str, Any]], now: datetime) -> Failure | None:
    end, limits = None, None
    for event in events:
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind in ("task_complete", "turn_complete", "turn_aborted"):
            end = payload
        elif kind == "token_count" and isinstance(payload.get("rate_limits"), dict):
            limits = payload["rate_limits"]
    if end is None or end.get("type") == "turn_aborted" or not isinstance(end.get("error"), dict):
        return None
    error = end["error"]
    message = " ".join(str(error.get("message") or "turn ended in error").split())
    info = error.get("codex_error_info")
    if isinstance(info, dict) and info:
        info = next(iter(info))  # a variant with fields, such as {"http_connection_failed": {...}}
    kind = CODEX_KINDS.get(str(info)) or classify(message)
    resets_at = None
    if kind == "quota":
        resets_at = _codex_reset(limits) or parse_reset(message, now)
    return Failure(kind, message, resets_at)


def _codex_reset(limits: dict[str, Any] | None) -> float | None:
    """The reset of the exhausted window in codex's last rate-limit snapshot."""

    if not limits:
        return None
    windows = [w for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)]
    full = [w for w in windows if (w.get("used_percent") or 0) >= 100 and w.get("resets_at")]
    if not full:
        return None
    return float(max(w["resets_at"] for w in full))


def _claude(events: list[dict[str, Any]], now: datetime) -> Failure | None:
    last = next((e for e in reversed(events) if e.get("type") == "assistant"), None)
    if last is None or not last.get("isApiErrorMessage"):
        return None
    message_obj = last.get("message") if isinstance(last.get("message"), dict) else {}
    texts = [
        part.get("text", "") for part in message_obj.get("content") or []
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    message = " ".join(" ".join(texts).split()) or str(last.get("error") or "API error")
    error = last.get("error")
    if message_obj.get("stop_reason") == "refusal":
        kind = "refusal"
    elif error == "rate_limit":
        kind = "quota" if classify(message) == "quota" else "transient"
    elif error == "authentication_failed":
        lower = message.lower()
        kind = "transient" if "another claude code process" in lower or "temporary" in lower else "auth"
    elif error == "billing_error":
        kind = "quota"
    elif error == "server_error":
        kind = "transient"
    else:
        kind = classify(message)
    resets_at = None
    if kind == "quota":
        resets_at = _epoch_in(last.get("quotaLimits")) or parse_reset(message, now)
    return Failure(kind, message, resets_at)


def _epoch_in(value: Any) -> float | None:
    """The latest reset time stored anywhere in claude's quota record, if any."""

    found: list[float] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif "reset" in key.lower() and isinstance(node, (int, float)) and node > 1_000_000_000:
            found.append(node / 1000 if node > 10_000_000_000 else float(node))

    walk(value)
    return max(found) if found else None


def _pi(events: list[dict[str, Any]], now: datetime) -> Failure | None:
    edited = {e.get("targetId") for e in events if e.get("type") == "context_edit"}
    last = None
    for event in events:
        message = event.get("message")
        if event.get("type") == "message" and isinstance(message, dict) and message.get("role") == "assistant":
            last = event
    if last is None or last.get("id") in edited:
        return None  # an error pi retried is history, not the turn's end
    message = last["message"]
    if message.get("stopReason") != "error":
        return None
    text = " ".join(str(message.get("errorMessage") or "turn ended in error").split())
    kind = classify(text)
    resets_at = None
    if kind == "quota":
        stamp = message.get("timestamp")
        when = datetime.fromtimestamp(stamp / 1000).astimezone() if isinstance(stamp, (int, float)) else now
        resets_at = parse_reset(text, when)
    return Failure(kind, text, resets_at)


# ---- screens -----------------------------------------------------------------------------

# Lines an agent draws for a failed turn: codex's "■ …" and "This content can’t be
# shown", claude's "API Error: …" and quota line, pi's "Error: …" and retry summary.
SCREEN_LINE = re.compile(
    r"^(?:■ |Error: |Retry failed after )|API Error|(?:You've|You’ve) hit your|can[’']t be shown"
)


def screen_failure(screen: str, now: datetime | None = None) -> Failure | None:
    """A failure shown at the bottom of an agent's screen: the backup when no
    transcript can be found."""

    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    for line in reversed(lines[-15:]):
        if SCREEN_LINE.search(line):
            kind = classify(line)
            resets_at = parse_reset(line, now or datetime.now().astimezone()) if kind == "quota" else None
            return Failure(kind, line, resets_at)
    return None
