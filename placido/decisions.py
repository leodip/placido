"""Decisions the user made during a run, kept beside the sealed agreement.

When an implementer or a fixer cannot go on without the user, it asks in its own
tab (a result starting `Blocked:`), the user answers there, and the agent records
the decision in `decisions.md` in the issue's folder before finishing. A pull
request's review puts its open questions to the user in a step of their own, and
placido records the answers. The sealed agreement is never edited: later slices and
the reviewer read both, and a decision takes precedence over the agreement where
they differ.

Each entry is a level-2 heading naming the step that asked, then the question,
the decision, and why:

    ## D1 · 03-implement-slice-2 · 2026-10-02
    **Question:** Which session store should logout clear?
    **Decision:** Both; the Redis one is authoritative.
    **Why:** The cookie store is a cache that can go stale.

A decision about a slice may also change the gates the agreement gave it, with a line
listing the slice's extra gates as they stand from then on, each in backticks, or
`none`. Placido runs those instead of the sealed ones, in the same attempt and in any
later one, so the agreement stays sealed:

    **Gates:** `unit --race`, `lint`

Decided 2026-11-03: on #718 slice 4's sealed gate could not run (`go test -race` in a
devcontainer that pins cgo off). The user chose another gate, but nothing could carry
the choice to placido, so the implementer edited the sealed slices and their seal by
hand, and the run still ran the old gate from memory until it was restarted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from placido import spec

FILE = "decisions.md"
HEADING = re.compile(r"^## D(\d+) · (\S+)", re.MULTILINE)
GATES = re.compile(r"^\*\*Gates:\*\*(.*)$", re.MULTILINE)
QUESTION = "question-{n}.md"  # an asked question, kept in the step folder


def path(run_dir: Path) -> Path:
    return spec.issue_dir(run_dir) / FILE


def entries(run_dir: Path) -> list[tuple[int, str]]:
    """(number, step) of each recorded decision, in order."""

    file = path(run_dir)
    text = file.read_text(encoding="utf-8") if file.is_file() else ""
    return [(int(number), step) for number, step in HEADING.findall(text)]


@dataclass(frozen=True)
class Gates:
    """A slice's extra gates as a decision set them."""

    decision: int
    gates: list[str]
    problem: str = ""  # why the line cannot be read, if it cannot


def gates(run_dir: Path, slice_id: Any) -> Gates | None:
    """The extra gates the latest decision about this slice set, or None if none did.
    A decision is about the slice when the step that recorded it implements it."""

    file = path(run_dir)
    text = file.read_text(encoding="utf-8") if file.is_file() else ""
    found = None
    marks = list(HEADING.finditer(text))
    for mark, following in zip(marks, [*marks[1:], None]):
        if not mark.group(2).endswith(f"-implement-slice-{slice_id}"):
            continue
        line = GATES.search(text, mark.end(), following.start() if following else len(text))
        if line is None:
            continue
        listed = re.findall(r"`([^`]+)`", line.group(1))
        bare = line.group(1).strip().rstrip(".").lower()
        problem = "" if listed or bare == "none" else (
            f"decision D{mark.group(1)}'s **Gates:** line names no gate. Write each gate in"
            " backticks, as `unit --race`, `lint`, or write none."
        )
        found = Gates(int(mark.group(1)), [g.strip() for g in listed], problem)
    return found


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


def prompt_line(run_dir: Path, over: str = "the agreement") -> str:
    """How every implement, fix, and review prompt points at the decisions. over: what
    a decision wins over, such as a pull request's final review."""

    file = path(run_dir)
    if file.is_file() and entries(run_dir):
        return (
            f"- **Decisions made with the user:** {file}. Read them; where one differs from"
            f" {over}, the decision wins."
        )
    return f"- **Decisions made with the user:** none yet ({file} when there are)."
