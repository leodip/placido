"""`placido report`: what a run spent and how it went, or trends across runs, read
from run folders without changing them.

A run's report is a timeline of its steps (agent, duration, outcome, tokens, and
anything notable such as questions or rejected results), then its slices, review,
the time it waited for the user, its usage by agent, and what went wrong. Across
runs, one line per run and the totals and averages that show trends, such as how
often agents are refused or how many attempts slices take.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from placido import runlog
from placido.usage import Usage, read, when


@dataclass
class StepRow:
    name: str
    role: str
    agent: str  # "claude opus high"
    started: datetime | None
    seconds: float | None
    outcome: str
    usage: Usage
    notes: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    path: Path
    issue: str
    title: str
    state: str
    closed: bool
    started: datetime | None
    seconds: float | None
    steps: list[StepRow]
    slices: int  # in the agreement
    committed: int
    attempts: int  # slice attempts, interruptions excluded
    rounds: int
    review: str  # the review's outcome, or "" when none ran
    findings: dict[str, int]
    fallbacks: list[str]
    refusals: int
    questions: int
    waited: float  # seconds spent waiting for the user's answers and dialogs
    usage: Usage
    by_agent: dict[str, Usage]


def build(run_dir: Path) -> RunReport:
    events = list(runlog.read_events(run_dir))
    try:
        record = runlog.read_record(run_dir)
    except (OSError, ValueError):
        record = {}
    steps = _steps(run_dir, events)
    usage, by_agent = Usage(), {}
    for row in steps:
        usage = usage.add(row.usage)
        for agent in row.usage.agents:
            by_agent[agent] = by_agent.get(agent, Usage()).add(row.usage)
    kinds = [e.get("event") for e in events]
    findings = {"blocking": 0, "significant": 0, "minor": 0}
    for event in events:
        if event.get("event") == "review.findings":
            for key in findings:
                findings[key] += int(event.get(key) or 0)
    done = runlog.last_event(run_dir, "review.done") or {}
    fallbacks = [
        f"{e.get('role')}: {e.get('from')} → {e.get('to')} ({e.get('reason')})"
        for e in events if e.get("event") == "agent.fallback"
    ]
    state = runlog.outcome(run_dir)
    created = runlog.last_event(run_dir, "worktree.created") or {}
    if state == "unfinished" and "run.closed" not in kinds and created.get("path") \
            and not Path(created["path"]).is_dir():
        state = "left (worktree gone)"  # removed outside placido, never closed
    return RunReport(
        path=run_dir,
        issue=run_dir.parent.name,
        title=str(record.get("issue_title") or ""),
        state="in progress" if state == "unfinished" else state,
        closed="run.closed" in kinds,
        started=when(events[0].get("ts")) if events else None,
        seconds=_between(events[0].get("ts"), events[-1].get("ts")) if events else None,
        steps=steps,
        slices=_slice_count(run_dir),
        committed=kinds.count("slice.committed"),
        attempts=kinds.count("slice.start"),
        rounds=kinds.count("review.round"),
        review=str(done.get("outcome") or ""),
        findings=findings,
        fallbacks=fallbacks,
        refusals=sum(1 for e in events if e.get("event") == "agent.failed" and e.get("kind") == "refusal"),
        questions=kinds.count("agent.question"),
        waited=_waited(events),
        usage=usage,
        by_agent=by_agent,
    )


def _steps(run_dir: Path, events: list[dict[str, Any]]) -> list[StepRow]:
    agents_by_name: dict[str, dict[str, Any]] = {}
    rows: dict[str, StepRow] = {}
    starts: dict[str, Any] = {}
    for event in events:
        kind, step = event.get("event"), event.get("step")
        if kind == "agent.start":
            agents_by_name[str(event.get("name"))] = event
        if not step:
            continue
        if kind == "step.start":
            starts[step] = event.get("ts")
            started = event
            rows[step] = StepRow(step, str(started.get("role", "")), "", when(event.get("ts")), None, "running", Usage())
            if event.get("resumed"):
                rows[step].agent = _describe(agents_by_name.get(str(event["resumed"]), {}))
                rows[step].notes.append("resumed")
        elif step not in rows:
            continue
        row = rows[step]
        if kind == "agent.start":
            row.agent = _describe(event)
        elif kind == "step.end":
            row.outcome = str(event.get("outcome", "?"))
            row.seconds = event.get("seconds")
            row.usage = _usage(run_dir / "steps" / step, row.agent.split(" ")[0], starts[step], event.get("ts"))
        elif kind == "agent.nudge":
            row.notes.append("nudged")
        elif kind == "result.rejected":
            row.notes.append("result sent back")
        elif kind == "agent.question":
            row.notes.append("asked you")
        elif kind == "agent.retry":
            row.notes.append(f"retried after {event.get('kind')}")
        elif kind == "agent.quota_wait":
            row.notes.append("waited for quota")
        elif kind == "agent.failed":
            row.notes.append(str(event.get("kind")))
        elif kind == "tab.kept":
            row.notes.append("tab kept")
    return list(rows.values())


def _describe(start: dict[str, Any]) -> str:
    return " ".join(str(start.get(k)) for k in ("agent", "model", "effort") if start.get(k))


def _usage(folder: Path, agent: str, start: Any, end: Any) -> Usage:
    begin, finish = when(start), when(end)
    if begin is None or finish is None:
        return Usage()
    return read(folder / "transcript.jsonl", agent, begin, finish)


def _waited(events: list[dict[str, Any]]) -> float:
    """Seconds spent waiting for the user: from each question to its answer, and from
    each dialog that needed them until it cleared."""

    total, opened = 0.0, {}
    pairs = {"agent.question": "agent.answered", "agent.blocked": "agent.unblocked"}
    for event in events:
        kind, step = event.get("event"), event.get("step")
        if kind in pairs:
            opened[(pairs[kind], step)] = event.get("ts")
        elif (kind, step) in opened:
            total += _between(opened.pop((kind, step)), event.get("ts")) or 0
    return total


def _slice_count(run_dir: Path) -> int:
    try:
        data = json.loads((run_dir.parent / "slices.json").read_text(encoding="utf-8"))
        return len(data["slices"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _between(start: Any, end: Any) -> float | None:
    a, b = when(start), when(end)
    return (b - a).total_seconds() if a and b else None


# ---- text ---------------------------------------------------------------------------


def render(report: RunReport) -> str:
    title = f" · {report.title}" if report.title else ""
    state = report.state + (" · closed" if report.closed else "")
    started = report.started.astimezone().strftime("%Y-%m-%d %H:%M") if report.started else "?"
    lines = [f"{report.issue}{title}", f"{state} · started {started} · {duration(report.seconds)}", ""]
    if report.steps:
        lines.append("Steps")
        width = max(len(row.name) for row in report.steps)
        agent_width = max(len(row.agent) for row in report.steps)
        for row in report.steps:
            clock = row.started.astimezone().strftime("%H:%M") if row.started else "     "
            notes = f"  {', '.join(row.notes)}" if row.notes else ""
            lines.append(
                f"  {clock}  {row.name.ljust(width)}  {row.agent.ljust(agent_width)}  "
                f"{duration(row.seconds).rjust(7)}  {row.outcome.ljust(8)}  {tokens(row.usage)}{notes}"
            )
        lines.append("")
    if report.slices or report.attempts:
        extra = report.attempts - report.committed
        retries = f", {extra} more attempt{'s' if extra != 1 else ''}" if extra > 0 else ""
        lines.append(f"Slices   {report.committed} of {report.slices} committed{retries}")
    if report.rounds:
        found = ", ".join(f"{n} {k}" for k, n in report.findings.items())
        rounds = f"{report.rounds} round{'s' if report.rounds != 1 else ''}"
        lines.append(f"Review   {report.review or 'unfinished'} in {rounds}; findings: {found}")
    if report.questions or report.waited:
        asked = f"{report.questions} question{'s' if report.questions != 1 else ''}"
        lines.append(f"You      {asked}; {duration(report.waited)} waiting for you")
    lines += _usage_lines(report)
    trouble = report.fallbacks + ([f"{report.refusals} refusal(s)"] if report.refusals else [])
    lines.append(f"Trouble  {'; '.join(trouble) if trouble else 'none'}")
    return "\n".join(lines) + "\n"


def _usage_lines(report: RunReport) -> list[str]:
    parts = [
        f"{agent} {count(usage.total)} tokens ({cached(usage)} cached, {count(usage.output)} out)"
        for agent, usage in sorted(report.by_agent.items())
    ]
    lines = [f"Usage    {'; '.join(parts) if parts else 'no transcripts'}"]
    if report.usage.cost:
        lines.append(f"         OpenRouter ${report.usage.cost:.2f}")
    codex = report.by_agent.get("codex")
    if codex and codex.window_last is not None:
        span = _window_name(codex.window_minutes)
        lines.append(f"         codex {span} window {codex.window_first:.0f}% → {codex.window_last:.0f}% used")
    return lines


def _window_name(minutes: int | None) -> str:
    return {300: "5-hour", 10080: "weekly"}.get(minutes or 0, f"{minutes}-minute" if minutes else "usage")


def tokens(usage: Usage) -> str:
    return f"{count(usage.total)} tok ({cached(usage)} cached)" if usage.total else "-"


def cached(usage: Usage) -> str:
    fed = usage.input + usage.cache_read + usage.cache_write
    return f"{100 * usage.cache_read / fed:.0f}%" if fed else "0%"


def count(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


# ---- across runs --------------------------------------------------------------------


def trends(reports: list[RunReport]) -> str:
    if not reports:
        return "no runs\n"
    width = max(len(r.issue) for r in reports)
    lines = ["Runs"]
    for r in reports:
        started = r.started.astimezone().strftime("%Y-%m-%d %H:%M") if r.started else "?"
        state = r.state + (", closed" if r.closed else "")
        lines.append(
            f"  {started}  {r.issue.ljust(width)}  {state.ljust(20)}  {duration(r.seconds).rjust(7)}  "
            f"slices {r.committed}/{r.slices}  attempts {r.attempts}  rounds {r.rounds}  "
            f"{count(r.usage.total)} tok"
        )
    finished = [r for r in reports if r.state not in ("in progress", "failed", "left (worktree gone)")]
    attempts = sum(r.attempts for r in reports)
    committed = sum(r.committed for r in reports)
    steps = sum(len(r.steps) for r in reports)
    lines += [
        "",
        f"Runs       {len(reports)}, {len(finished)} finished"
        + (f"; median {duration(median(r.seconds or 0 for r in finished))}" if finished else ""),
        f"Slices     {committed} committed in {attempts} attempts"
        + (f" ({attempts / committed:.1f} per slice)" if committed else ""),
        f"Reviews    {sum(1 for r in reports if r.rounds)} run(s), {sum(r.rounds for r in reports)} rounds; "
        f"findings: " + ", ".join(f"{sum(r.findings[k] for r in reports)} {k}" for k in ("blocking", "significant", "minor")),
        f"Refusals   {sum(r.refusals for r in reports)} in {steps} steps; "
        f"{sum(len(r.fallbacks) for r in reports)} fallback(s)",
        f"You        {sum(r.questions for r in reports)} question(s); {duration(sum(r.waited for r in reports))} waiting",
    ]
    total = Usage()
    for r in reports:
        total = total.add(r.usage)
    lines.append(f"Usage      {count(total.total)} tokens ({cached(total)} cached)"
                 + (f"; OpenRouter ${total.cost:.2f}" if total.cost else ""))
    return "\n".join(lines) + "\n"


# ---- JSON ---------------------------------------------------------------------------


def as_dict(report: RunReport) -> dict[str, Any]:
    def stamp(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "run": str(report.path), "issue": report.issue, "title": report.title, "state": report.state,
        "closed": report.closed, "started": stamp(report.started), "seconds": report.seconds,
        "steps": [
            {"name": s.name, "role": s.role, "agent": s.agent, "started": stamp(s.started),
             "seconds": s.seconds, "outcome": s.outcome, "usage": s.usage.as_dict(), "notes": s.notes}
            for s in report.steps
        ],
        "slices": report.slices, "committed": report.committed, "attempts": report.attempts,
        "review": {"outcome": report.review, "rounds": report.rounds, "findings": report.findings},
        "fallbacks": report.fallbacks, "refusals": report.refusals, "questions": report.questions,
        "waited_seconds": report.waited, "usage": report.usage.as_dict(),
        "by_agent": {agent: usage.as_dict() for agent, usage in report.by_agent.items()},
    }
