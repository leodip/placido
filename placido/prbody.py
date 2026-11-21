"""The pull request's body, written for the person reviewing the change: what it does,
the decisions behind it, what the review found, and where the follow-ups are. The
run's own story (attempts, waits, tabs, times) stays in summary.md.

Decided with the user on 2026-10-02, after the first real pull request read as a run
log with "Follow-ups" three times and never said what the change does. The agreement's
release notes, what upgrading asks of the people who run the project, go as written
into a comment of their own, which the body links to (decided 2026-10-24, after an
agreement promised notes no step could write).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from placido import checks, decisions, implement, runlog, spec

CI_WORDS = {
    "red": "CI is red after placido's fixes",
    "pending": "CI was still running when placido stopped waiting",
}
LAST_GATES_FAIL = "The last gates fail after placido's fixes"
LEAD = re.compile(r"^\d+\.\s+\*\*(.+?)\*\*", re.MULTILINE | re.DOTALL)
FINDING = re.compile(r"^- \*\*(?P<id>[^*]+)\*\* (?P<severity>\w+) · (?P<rest>.*)$")
REVIEW_KEPT = ("Needs your decision", "Fixed, not verified by a later round")
MEASURED = re.compile(r"^\s*- \*\*Benchmark:\*\*\s*(.+)$", re.M)  # a slice's evidence line


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
    last = next((e for e in reversed(events) if e.get("event") == "last_gates.gave_up"), None)
    if last:
        failed = f" ({', '.join(last.get('gates') or [])})" if last.get("gates") else ""
        why = checks.outside(run_dir, "the last gates")
        lines += [f"> **{LAST_GATES_FAIL}{failed}.**" + (f" Nothing in this change causes it: {why}" if why else ""), ""]
    solution = section(agreement, "Solution")
    if solution:
        lines += ["## What this changes", "", solution, ""]
    lines += _benchmark(run_dir)
    lines += _flaky(run_dir, events)
    noted = next((e for e in reversed(events) if e.get("event") == "release_notes.posted"), None)
    if noted:
        lines += ["## Release notes", "", f"What upgrading asks of the people who run the project:"
                  f" [the release notes comment]({noted.get('url')}).", ""]
    lines += _decisions(agreement, run_dir, events)
    lines += _review(run_dir)
    lines += review_comments(events)
    lines += _follow_ups(events)
    lines += ["---", f"Built by placido · run `{run_dir.parent.name}/{run_dir.name}`"]
    return "\n".join(lines).strip() + "\n"


def release_notes(run_dir: Path) -> str:
    """The release notes comment: the agreement's section as written, or nothing."""

    notes = section(_read(spec.issue_dir(run_dir) / "agreement.md"), "Release notes")
    return f"## Release notes\n\n{notes}\n" if notes else ""


def _benchmark(run_dir: Path) -> list[str]:
    """The agreement's benchmark, then what each slice measured on it."""

    agreed = spec.benchmark(spec.issue_dir(run_dir))
    if not agreed:
        return []
    measured = []
    for result in implement.slice_results(run_dir):
        found = MEASURED.search(_read(result))
        number = re.search(r"implement-slice-(\d+)", result.parent.name)
        if found and number:
            measured.append(f"- Slice {number.group(1)}: {found.group(1).strip()}")
    lines = ["## Benchmark", "", agreed, ""]
    if measured:
        lines += ["Measured by the slices, before → after:", "", *measured, ""]
    return lines


def flaky(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The flaky tests of the final and last gates (checks.flaky), as their stages' latest
    outcomes, passed or gave up, recorded them, each note with its kind; each log once, as
    a last gate reused from the same final gate's pass has its log."""

    by_log: dict[str, dict[str, Any]] = {}
    for kind in ("final", "last"):
        ends = (f"{kind}_gates.passed", f"{kind}_gates.gave_up")
        outcome = next((e for e in reversed(events) if e.get("event") in ends), None) or {}
        for note in outcome.get("flaky") or []:
            by_log.setdefault(str(note.get("log")), {**note, "kind": kind})
    return list(by_log.values())


def flaky_gate(note: dict[str, Any]) -> str:
    """Which gate a flaky note is about, in words: "the last gate `e2e`"."""

    return f"the {note.get('kind')} gate `{note.get('gate')}`"


def _flaky(run_dir: Path, events: list[dict[str, Any]]) -> list[str]:
    """Each gate that passed only once it ran some tests again, with those tests, up to
    the gate.FLAKY_KEPT placido kept, and where its log is in the run's folder: the
    reviewer would not see them otherwise, as the gate passed."""

    notes = flaky(events)
    if not notes:
        return []
    lines = ["## Flaky tests", ""]
    for note in notes:
        which = flaky_gate(note)
        which = which[:1].upper() + which[1:]
        log = Path(str(note.get("log") or ""))
        where = log.relative_to(run_dir) if log.is_relative_to(run_dir) else log.name
        tests = [str(t).replace("`", "'") for t in note.get("tests") or []]
        if not tests:
            lines += [f"{which} passed only once it ran some of its tests again;"
                      f" its log, `{where}` in the run's folder, does not name them.", ""]
            continue
        lines += [f"{which} passed only once it ran these tests again, which failed"
                  f" on its first run. Its log: `{where}`, in the run's folder.", ""]
        lines += [f"- `{test}`" for test in tests]
        more = int(note.get("count") or 0) - len(tests)
        if more > 0:
            lines.append(f"- … and {more} more, in the log.")
        lines.append("")
    return lines


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


section = spec.section


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


COMMENT_WORDS = (
    ("fixed", "fixed"),
    ("not-changed", "not changed (each reply says why)"),
    ("follow-up", "left out as follow-ups"),
    ("left", "left for you"),
)


def review_comments(events: list[dict[str, Any]], level: str = "##") -> list[str]:
    """How placido answered the comments the project's reviewers left on the pull
    request (placido/comments.py): the counts, and what is left for the user."""

    settled = [e for e in events if e.get("event") == "comments.settled"]
    unanswered = next((e for e in reversed(events) if e.get("event") == "comments.unanswered"), None)
    if not settled and not unanswered:
        return []
    answers = [a for e in settled for a in e.get("answers", []) if isinstance(a, dict)]
    lines = [f"{level} Review comments", ""]
    if answers:
        authors = sorted({str(a.get("author")) for a in answers})
        passes = len({e.get("pass") for e in settled})
        parts = [f"{n} {words}" for kind, words in COMMENT_WORDS
                 if (n := sum(1 for a in answers if a.get("outcome") == kind))]
        lines.append(f"Placido answered {len(answers)} comment{'s' if len(answers) != 1 else ''} from"
                     f" {', '.join(authors)}" + (f", in {passes} passes" if passes > 1 else "")
                     + ": " + ", ".join(parts) + ".")
        left = [a for a in answers if a.get("outcome") == "left"]
        if left:
            lines += ["", f"{level}# Left for you", ""]
            lines += [f"- [{a.get('title')}]({a.get('url')}): {a.get('text')}" for a in left]
    if unanswered:
        if answers:
            lines.append("")
        count = int(unanswered.get("count") or 0)
        lines.append(f"{count} comment{'s' if count != 1 else ''} came after placido's last pass, unanswered: "
                     + ", ".join(str(url) for url in unanswered.get("urls", [])) + ".")
    return lines + [""]


def _follow_ups(events: list[dict[str, Any]]) -> list[str]:
    posted = next((e for e in events if e.get("event") == "followups.posted"), None)
    noted = [e for e in events if e.get("event") == "issue.noted"]
    if not posted and not noted:
        return []
    lines = ["## Follow-ups", ""]
    if posted:
        count = int(posted.get("count") or 0)
        more = []
        for event in events:
            if event.get("event") == "followups.updated":
                count = int(event.get("count") or 0)
            elif event.get("event") == "followups.continued":
                count += int(event.get("count") or 0)
                more.append(event.get("url"))
        what = f"{count} piece{'s' if count != 1 else ''} of work" if count else "The work"
        where = f"[the follow-ups comment]({posted.get('url')})" + "".join(
            f" and [its continuation]({url})" if len(more) == 1 else f", [continued]({url})" for url in more
        )
        lines.append(f"{what} left out of this change, each drafted as an issue with the command"
                     f" that files it: {where}.")
    if noted:
        if posted:
            lines.append("")
        lines.append("Noted on " + ", ".join(f"[#{e.get('issue')}]({e.get('url')})" for e in noted)
                     + ", which this change affects.")
    return lines + [""]
