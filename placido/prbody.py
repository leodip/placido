"""The pull request's body, written for the person reviewing the change: what it does,
the decisions behind it, what the review found, and where the follow-ups are. The
run's own story (attempts, waits, tabs, times) stays in summary.md.

Decided with the user on 2026-10-02, after the first real pull request read as a run
log with "Follow-ups" three times and never said what the change does.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from placido import decisions, runlog, spec

CI_WORDS = {
    "red": "CI is red after placido's fixes",
    "pending": "CI was still running when placido stopped waiting",
}
LEAD = re.compile(r"^\d+\.\s+\*\*(.+?)\*\*", re.MULTILINE | re.DOTALL)
FINDING = re.compile(r"^- \*\*(?P<id>[^*]+)\*\* (?P<severity>\w+) · (?P<rest>.*)$")
REVIEW_KEPT = ("Needs your decision", "Fixed, not verified by a later round")


def build(run_dir: Path, folded: list[int]) -> str:
    record = runlog.read_record(run_dir)
    events = list(runlog.read_events(run_dir))
    agreement = _read(spec.issue_dir(run_dir) / "agreement.md")
    closes = []
    if record.get("issue_number"):
        closes.append(f"Closes #{record['issue_number']}")
    closes += [f"Closes #{n}" for n in folded if n != record.get("issue_number")]
    lines = closes + [""]
    ci = next((e for e in reversed(events) if e.get("event") == "ci.result"), None)
    if ci and ci.get("outcome") in CI_WORDS:
        failed = f" ({', '.join(ci.get('failed') or [])})" if ci.get("failed") else ""
        lines += [f"> **{CI_WORDS[ci['outcome']]}{failed}.**", ""]
    solution = section(agreement, "Solution")
    if solution:
        lines += ["## What this changes", "", solution, ""]
    lines += _decisions(agreement, run_dir, events)
    lines += _review(run_dir)
    lines += _follow_ups(events)
    lines += ["---", f"Built by placido · run `{run_dir.parent.name}/{run_dir.name}`"]
    return "\n".join(lines).strip() + "\n"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def section(text: str, name: str, level: int = 2) -> str:
    """A markdown section's text, without its heading."""

    marks = "#" * level
    found = re.search(rf"^{marks} {re.escape(name)}\b[^\n]*\n(.*?)(?=^#{{1,{level}}} |\Z)", text, re.M | re.S)
    return found.group(1).strip() if found else ""


def leads(decided: str) -> list[str]:
    """Each numbered decision's bold first sentence, on one line."""

    return [" ".join(lead.split()) for lead in LEAD.findall(decided)]


def _decisions(agreement: str, run_dir: Path, events: list[dict[str, Any]]) -> list[str]:
    agreed = leads(section(agreement, "Decisions"))
    steps = {e.get("step") for e in events if e.get("event") == "step.start"}
    file = decisions.path(run_dir)
    during = []
    if file.is_file():
        blocks = re.split(r"(?=^## D\d+ · )", file.read_text(encoding="utf-8"), flags=re.MULTILINE)
        for block in blocks:
            heading = decisions.HEADING.match(block)
            if not heading or heading.group(2) not in steps:
                continue
            decided = re.search(r"^\*\*Decision:\*\*\s*(.+)$", block, re.MULTILINE)
            during.append(f"D{heading.group(1)}: {decided.group(1).strip() if decided else block.splitlines()[0][3:]}")
    if not agreed and not during:
        return []
    lines = ["## Decisions", ""]
    lines += [f"{n}. {lead}" for n, lead in enumerate(agreed, 1)]
    if during:
        lines += ["", "Decided during the run:", ""] + [f"- {item}" for item in during]
    return lines + [""]


def _review(run_dir: Path) -> list[str]:
    """What the review found and how each finding ended, from the account it wrote."""

    text = _read(run_dir / "review.md")
    if not text:
        return []
    outcome = text.splitlines()[0].removeprefix("# Review:").strip()
    rounds = len(re.findall(r"^- Round \d+", section(text, "Rounds"), re.M))
    found: list[str] = []
    lines = section(text, "Findings").splitlines()
    for n, line in enumerate(lines):
        match = FINDING.match(line)
        if not match:
            continue
        title = match.group("rest").rsplit(" · ", 1)[-1]
        story = lines[n + 1].strip() if n + 1 < len(lines) and lines[n + 1].startswith("  ") else ""
        found.append(f"- **{match.group('severity')}** {title}" + (f": {story}" if story else ""))
    plural = "s" if rounds != 1 else ""
    if outcome == "passed":
        head = f"Passed in {rounds} round{plural}" + ("." if found else ", with no findings.")
    elif outcome == "escalated":
        head = f"Ended after {rounds} round{plural} with findings that need your decision, below."
    else:
        head = f"{outcome.capitalize()} after {rounds} round{plural}."
    out = ["## Review", "", head]
    if found:
        out += ["", *found]
    for name in REVIEW_KEPT:
        kept = section(text, name)
        if kept:
            out += ["", f"### {name}", "", kept]
    return out + [""]


def _follow_ups(events: list[dict[str, Any]]) -> list[str]:
    posted = next((e for e in reversed(events) if e.get("event") == "followups.posted"), None)
    noted = [e for e in events if e.get("event") == "issue.noted"]
    if not posted and not noted:
        return []
    lines = ["## Follow-ups", ""]
    if posted:
        count = posted.get("count")
        what = f"{count} piece{'s' if count != 1 else ''} of work" if count else "The work"
        lines.append(f"{what} left out of this change, each drafted as an issue with the command"
                     f" that files it: [the follow-ups comment]({posted.get('url')}).")
    if noted:
        if posted:
            lines.append("")
        lines.append("Noted on " + ", ".join(f"[#{e.get('issue')}]({e.get('url')})" for e in noted)
                     + ", which this change affects.")
    return lines + [""]
