"""Decisions the user made during a run, kept beside the sealed agreement.

When an implementer or a fixer cannot go on without the user, it asks in its own
tab (a result starting `Blocked:`), the user answers there, and the agent records
the decision in `decisions.md` in the issue's folder before finishing. The sealed
agreement is never edited: later slices and the reviewer read both, and a decision
takes precedence over the agreement where they differ.

Each entry is a level-2 heading naming the step that asked, then the question,
the decision, and why:

    ## D1 · 03-implement-slice-2 · 2026-10-02
    **Question:** Which session store should logout clear?
    **Decision:** Both; the Redis one is authoritative.
    **Why:** The cookie store is a cache that can go stale.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from placido import spec

FILE = "decisions.md"
HEADING = re.compile(r"^## D(\d+) · (\S+)", re.MULTILINE)
QUESTION = "question-{n}.md"  # an asked question, kept in the step folder


def path(run_dir: Path) -> Path:
    return spec.issue_dir(run_dir) / FILE


def entries(run_dir: Path) -> list[tuple[int, str]]:
    """(number, step) of each recorded decision, in order."""

    file = path(run_dir)
    text = file.read_text(encoding="utf-8") if file.is_file() else ""
    return [(int(number), step) for number, step in HEADING.findall(text)]


def asked(folder: Path) -> int:
    """How many questions the step in this folder has asked the user."""

    return len(list(folder.glob(QUESTION.format(n="*"))))


def check(folder: Path, run_dir: Path) -> list[str]:
    """A step that asked the user must record each decision before it finishes."""

    questions = asked(folder)
    recorded = sum(1 for _, step in entries(run_dir) if step == folder.name)
    if recorded >= questions:
        return []
    numbers = [number for number, _ in entries(run_dir)]
    following = (max(numbers) + 1) if numbers else 1
    return [
        f"you asked the user {questions} question{'s' if questions != 1 else ''} but recorded"
        f" {recorded} decision{'s' if recorded != 1 else ''}. Append each to {path(run_dir)} as"
        f" '## D{following} · {folder.name} · {date.today().isoformat()}' followed by"
        " **Question:**, **Decision:**, and **Why:** lines."
    ]


def record(run_dir: Path, step: str, question: str, decision: str, why: str) -> int:
    """Append a decision placido itself took from the user, such as a follow-up the
    user chose to fold in; returns its number."""

    numbers = [number for number, _ in entries(run_dir)]
    number = (max(numbers) + 1) if numbers else 1
    file = path(run_dir)
    lead = "\n" if file.is_file() and file.read_text(encoding="utf-8").strip() else ""
    with file.open("a", encoding="utf-8") as out:
        out.write(
            f"{lead}## D{number} · {step} · {date.today().isoformat()}\n"
            f"**Question:** {question}\n**Decision:** {decision}\n**Why:** {why}\n"
        )
    return number


def prompt_line(run_dir: Path) -> str:
    """How every implement, fix, and review prompt points at the decisions."""

    file = path(run_dir)
    if file.is_file() and entries(run_dir):
        return (
            f"- **Decisions made with the user:** {file}. Read them; where one differs from"
            " the agreement, the decision wins."
        )
    return f"- **Decisions made with the user:** none yet ({file} when there are)."
