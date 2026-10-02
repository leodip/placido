"""The closing account of a run, `summary.md` in the run folder, written by placido
from the event log and the run's files, with no agent.

It says what the run did and what is left: each slice and its commit, the decisions
the user made, the review, follow-ups, and anything that went wrong on the way
(failed attempts, fallbacks, waits). Step 13 turns it into the pull request's body.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from placido import decisions, runlog, spec

CI_WORDS = {
    "green": "green",
    "red": "red after placido's fixes",
    "none": "no checks ran",
    "pending": "still running when placido stopped waiting",
}
FOLLOW_UPS = re.compile(r"^## Follow-ups\s*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def write(run_dir: Path, stopped: str | None = None) -> str:
    """Write the run's summary.md and return it. stopped: why a run that has not
    ended stopped, such as gave-up, for its state line."""

    events = list(runlog.read_events(run_dir))
    record = _record(run_dir)
    title = record.get("issue_title") or run_dir.parent.name
    lines = [f"# {title}", ""]
    lines += _facts(run_dir, record, events, stopped)
    lines += _slices(run_dir, events)
    lines += _decisions(run_dir, events)
    lines += _review(run_dir)
    lines += _posted_follow_ups(events)
    lines += _follow_ups(run_dir, events)
    lines += _trouble(events)
    text = "\n".join(lines).rstrip() + "\n"
    (run_dir / "summary.md").write_text(text, encoding="utf-8")
    return text


def _record(run_dir: Path) -> dict[str, Any]:
    try:
        return runlog.read_record(run_dir)
    except (OSError, ValueError):
        return {}


def _facts(
    run_dir: Path, record: dict[str, Any], events: list[dict[str, Any]], stopped: str | None,
) -> list[str]:
    state = runlog.outcome(run_dir)
    if state == "unfinished":
        state = f"stopped ({stopped}); run placido again to resume" if stopped else "in progress"
    facts = [f"- **State:** {state}"]
    if record.get("issue_source"):
        facts.append(f"- **Issue:** {record['issue_source']}")
    if record.get("branch"):
        base = str(record.get("base_commit", ""))[:7]
        facts.append(f"- **Branch:** `{record['branch']}`, from `{record.get('base', '?')}` at {base}")
    opened = next((e for e in reversed(events) if e.get("event") == "pr.opened"), None)
    if opened:
        facts.append(f"- **Pull request:** {opened.get('url')}")
    ci = next((e for e in reversed(events) if e.get("event") == "ci.result"), None)
    if ci:
        failed = f" ({', '.join(ci.get('failed') or [])})" if ci.get("failed") else ""
        facts.append(f"- **CI:** {CI_WORDS.get(ci.get('outcome'), ci.get('outcome'))}{failed}")
    if events:
        took = _seconds(events[0].get("ts"), events[-1].get("ts"))
        facts.append(f"- **Time:** {str(events[0].get('ts', ''))[:16].replace('T', ' ')}"
                     + (f", {_duration(took)} of activity" if took is not None else ""))
    return facts + [""]


def _slices(run_dir: Path, events: list[dict[str, Any]]) -> list[str]:
    try:
        entries = spec.load_slices(spec.issue_dir(run_dir))
    except (OSError, ValueError, KeyError):
        entries = []
    if not entries:
        return []
    committed = {e.get("slice"): e for e in events if e.get("event") == "slice.committed"}
    failed: dict[Any, list[str]] = {}
    for event in events:
        if event.get("event") == "slice.failed":
            failed.setdefault(event.get("slice"), []).append(str(event.get("outcome")))
    lines = ["## Slices", ""]
    for entry in entries:
        done = committed.get(entry["id"])
        state = f"`{str(done.get('commit', ''))[:7]}`" if done else "not built"
        tries = failed.get(entry["id"], [])
        extra = f" (after {len(tries)} failed attempt{'s' if len(tries) != 1 else ''}: {', '.join(tries)})" \
            if tries else ""
        lines.append(f"- {entry['id']}. {entry['title']}: {state}{extra}")
    return lines + [""]


def _decisions(run_dir: Path, events: list[dict[str, Any]]) -> list[str]:
    """This run's decisions: those recorded by its own steps."""

    file = decisions.path(run_dir)
    steps = {e.get("step") for e in events if e.get("event") == "step.start"}
    if not file.is_file():
        return []
    blocks = re.split(r"(?=^## D\d+ · )", file.read_text(encoding="utf-8"), flags=re.MULTILINE)
    mine = [b.strip() for b in blocks if (m := decisions.HEADING.match(b)) and m.group(2) in steps]
    if not mine:
        return []
    lines = ["## Decisions you made", ""]
    for block in mine:
        heading, *rest = block.splitlines()
        lines.append(f"- **{heading[3:].strip()}**")
        lines += [f"  {line.strip()}" for line in rest if line.strip()]
    return lines + [""]


def _review(run_dir: Path) -> list[str]:
    """The review's account, as the review wrote it, one heading level down."""

    file = run_dir / "review.md"
    if not file.is_file():
        return ["## Review", "", "Not run yet.", ""]
    text = file.read_text(encoding="utf-8").strip()
    return [re.sub(r"^(#+) ", r"#\1 ", line) for line in text.splitlines()] + [""]


def _follow_ups(run_dir: Path, events: list[dict[str, Any]]) -> list[str]:
    """What the implementers said was worth doing later, from each committed slice."""

    items: list[str] = []
    steps = run_dir / "steps"
    for event in events:
        if event.get("event") != "slice.committed":
            continue
        folders = sorted(steps.glob(f"*-implement-slice-{event.get('slice')}")) if steps.is_dir() else []
        result = next((f / "result.md" for f in reversed(folders) if (f / "result.md").is_file()), None)
        found = FOLLOW_UPS.search(result.read_text(encoding="utf-8")) if result else None
        if not found:
            continue
        for item in _bullets(found.group(1)):
            if item.rstrip(".").lower() != "none":
                items.append(f"- Slice {event.get('slice')}: {item}")
    return (["## Follow-ups from the slices", "", *items, ""]) if items else []


def _bullets(text: str) -> list[str]:
    """A markdown list's items, each joined back into one line: agents wrap long items."""

    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in "-*" or not items:
            items.append(stripped.lstrip("-*").strip())
        else:
            items[-1] = f"{items[-1]} {stripped}"
    return [item for item in items if item]


def _posted_follow_ups(events: list[dict[str, Any]]) -> list[str]:
    """Where the follow-ups were posted for the user to file by hand."""

    posted = next((e for e in reversed(events) if e.get("event") == "followups.posted"), None)
    if posted is None:
        return []
    counts = [f"{len(posted.get(key) or [])} {name}" for key, name in
              (("drafts", "from the interview"), ("review", "from the review")) if posted.get(key)]
    return ["## Follow-ups", "", f"Posted for filing by hand ({', '.join(counts)}): {posted.get('url')}", ""]


def _trouble(events: list[dict[str, Any]]) -> list[str]:
    """Anything that did not go smoothly: fallbacks, failed turns, waits, kept tabs."""

    lines = []
    for event in events:
        kind = event.get("event")
        if kind == "agent.fallback":
            lines.append(f"- {event.get('role')} moved from {event.get('from')} to {event.get('to')}"
                         f" after a {event.get('reason')}: {_short(event.get('message'))}")
        elif kind == "agent.chain_exhausted":
            lines.append(f"- {event.get('role')}: every agent gave a {event.get('kind')}")
        elif kind == "agent.failed":
            lines.append(f"- {event.get('step')}: {event.get('kind')} ({_short(event.get('message'))})")
        elif kind == "agent.quota_wait":
            lines.append(f"- {event.get('step')}: waited {_duration(event.get('seconds'))} for quota")
        elif kind == "agent.retry":
            lines.append(f"- {event.get('step')}: retried after a {event.get('kind')} error")
        elif kind == "checks.committed":
            lines.append(f"- fixed failing checks ({event.get('label')}) in `{str(event.get('commit', ''))[:7]}`")
        elif kind == "tab.kept":
            lines.append(f"- {event.get('step')}: tab kept open ({event.get('reason')})")
    return (["## Along the way", "", *lines, ""]) if lines else []


def _short(text: Any, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _seconds(start: Any, end: Any) -> float | None:
    try:
        return (datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds()
    except ValueError:
        return None


def _duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {total % 60:02d}s"
