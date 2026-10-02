"""What a step's agent used, read from the transcript copied into its step folder.

Only what happened between the step's start and end counts: a resumed reviewer's
transcript holds every earlier round too. Each agent records usage its own way:

- claude: `usage` on each assistant entry, the same reply logged several times,
  so each message id is counted once.
- codex: running totals in `token_count` events, so a step's share is the last
  total in its window minus the last one before it; the same events carry the
  ChatGPT usage window's `used_percent`.
- pi: `usage` on each assistant message, with the dollars OpenRouter charged.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Usage:
    input: int = 0  # fresh input tokens, not read from the cache
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0  # including reasoning
    reasoning: int = 0  # the part of output spent thinking, where the agent reports it
    cost: float = 0.0  # real dollars, OpenRouter only
    # codex's usage window, read at the first and last moment the usage covers. It is
    # reported in whole percent of a window that can span a week, so one step rarely
    # moves it: it is shown across a run, not per step.
    window_first: float | None = None
    window_last: float | None = None
    window_minutes: int | None = None
    agents: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output

    def add(self, other: "Usage") -> "Usage":
        """The two together, other being the later."""

        return Usage(
            self.input + other.input, self.cache_read + other.cache_read,
            self.cache_write + other.cache_write, self.output + other.output,
            self.reasoning + other.reasoning, self.cost + other.cost,
            self.window_first if self.window_first is not None else other.window_first,
            other.window_last if other.window_last is not None else self.window_last,
            other.window_minutes or self.window_minutes,
            self.agents | other.agents,
        )

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["agents"] = sorted(self.agents)
        data["total"] = self.total
        return data


def when(value: Any) -> datetime | None:
    """A timestamp as an aware datetime: ISO text, or epoch seconds or milliseconds."""

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def read(transcript: Path, agent: str, start: datetime, end: datetime) -> Usage:
    """The agent's usage between start and end, from its transcript."""

    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return Usage()
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    reader = {"claude": _claude, "codex": _codex, "pi": _pi}.get(agent)
    usage = reader(events, start, end) if reader else Usage()
    if usage.total or usage.cost:
        usage.agents = {agent}
    return usage


def _inside(event: dict[str, Any], start: datetime, end: datetime, key: str = "timestamp") -> bool:
    stamp = when(event.get(key))
    return stamp is not None and start <= stamp <= end


def _claude(events: list[dict[str, Any]], start: datetime, end: datetime) -> Usage:
    replies: dict[str, dict[str, Any]] = {}
    for event in events:
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict) or event.get("isApiErrorMessage"):
            continue
        if not isinstance(message.get("usage"), dict) or not _inside(event, start, end):
            continue
        # The same reply is logged once per content block; its usage is the reply's.
        replies[str(message.get("id") or id(event))] = message["usage"]
    usage = Usage()
    for counts in replies.values():
        usage.input += int(counts.get("input_tokens") or 0)
        usage.cache_write += int(counts.get("cache_creation_input_tokens") or 0)
        usage.cache_read += int(counts.get("cache_read_input_tokens") or 0)
        usage.output += int(counts.get("output_tokens") or 0)
        details = counts.get("output_tokens_details") or {}
        usage.reasoning += int(details.get("thinking_tokens") or 0)
    return usage


def _codex(events: list[dict[str, Any]], start: datetime, end: datetime) -> Usage:
    before: dict[str, Any] = {}
    last: dict[str, Any] | None = None
    usage = Usage()
    for event in events:
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        stamp = when(event.get("timestamp"))
        if stamp is None or stamp > end:
            continue
        info = payload.get("info") or {}
        totals = info.get("total_token_usage") if isinstance(info, dict) else None
        window = _window(payload.get("rate_limits"))
        if stamp < start:
            before = totals or before
            continue
        last = totals or last
        if window is not None:
            percent, minutes = window
            if usage.window_first is None:
                usage.window_first = percent
            usage.window_last, usage.window_minutes = percent, minutes
    if last:
        def delta(key: str) -> int:
            return max(0, int(last.get(key) or 0) - int(before.get(key) or 0))

        cached = delta("cached_input_tokens")
        usage.input = max(0, delta("input_tokens") - cached)  # codex counts cached input inside input
        usage.cache_read = cached
        usage.cache_write = delta("cache_write_input_tokens")
        usage.output = delta("output_tokens")
        usage.reasoning = delta("reasoning_output_tokens")
    return usage


def _window(limits: Any) -> tuple[float, int | None] | None:
    """The used percentage of codex's primary usage window, and its length in minutes."""

    if not isinstance(limits, dict):
        return None
    primary = limits.get("primary")
    if isinstance(primary, dict) and primary.get("used_percent") is not None:
        minutes = primary.get("window_minutes")
        return float(primary["used_percent"]), int(minutes) if minutes else None
    return None


def _pi(events: list[dict[str, Any]], start: datetime, end: datetime) -> Usage:
    usage = Usage()
    for event in events:
        message = event.get("message")
        if event.get("type") != "message" or not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if not isinstance(message.get("usage"), dict) or not _inside(event, start, end):
            continue
        counts = message["usage"]
        usage.input += int(counts.get("input") or 0)
        usage.cache_read += int(counts.get("cacheRead") or 0)
        usage.cache_write += int(counts.get("cacheWrite") or 0)
        usage.output += int(counts.get("output") or 0)
        usage.reasoning += int(counts.get("reasoning") or 0)
        cost = counts.get("cost")
        if isinstance(cost, dict):
            usage.cost += float(cost.get("total") or 0)
    return usage
