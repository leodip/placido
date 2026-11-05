"""The agreement's check against the code, before it is sealed.

The interview's agent writes claims about the code into the agreement, and the run
builds on them while the user sleeps. On #783 a decision said a missing setting is
refused with `VALIDATION_ERROR` "as" two other settings are, which they are not, and
the fixer stopped at night to ask which half of the decision to keep (2026-11-05).
So once the agreement passes its own checks, another agent reads it against the code:
claims about today's code and behavior, the precedents it names, and the places it
contradicts itself. Its findings go back to the interview while the user is still
there, and the agreement is sealed once the interview has settled them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from placido import config, spec
from placido.step import Step

SKILL = Path(__file__).resolve().parent.parent / "skills" / "vet" / "SKILL.md"
NOTES = Path(".placido") / "notes" / "vet.md"
FILE = "vet.md"
FINDINGS = "## Findings"
FINDING = re.compile(r"^### V\d+\b", re.M)

# What the interview's agent is told; Step's FIX puts it in a list and asks it to fix
# the problems and write its result again.
SETTLE = (
    "An independent check read the agreement against the code and found {count}, in {file}."
    " Settle each one before the agreement is sealed. Where the agreement says something about"
    " the code that is not so, or contradicts itself, and the correction leaves what the user"
    " decided as it was, correct agreement.md (and slices.json). Where the correction would"
    " change a decision, or the answer is a choice, ask the user as an interview question first."
    " Where you think a finding is wrong, tell the user why and leave the agreement as it is."
    " Then tell the user, briefly, how each finding was settled"
)


def prompt(run_dir: Path, worktree: Path, folder: Path) -> str:
    """The task for the vet agent: the skill, the agreement and the code, and its file."""

    issue = spec.issue_dir(run_dir)
    notes = worktree / NOTES
    lines = [
        f"# Check the agreement for {issue.name}",
        "",
        f"Follow the placido vet skill in {SKILL}.",
        "",
        f"- **The agreement:** {issue / 'agreement.md'} and {issue / 'slices.json'}.",
        f"- **The issue:** {run_dir / 'issue.md'}.",
        f"- **The code:** {worktree}. Read it; change nothing.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        f"- **Write:** {folder / FILE}.",
    ]
    return "\n".join(lines) + "\n"


def check(result: Path) -> list[str]:
    vetted = result.parent / FILE
    text = vetted.read_text(encoding="utf-8") if vetted.is_file() else ""
    if not text.strip():
        return [f"write your findings to {vetted} first, as the skill shows."]
    if not re.search(rf"^{FINDINGS}\s*$", text, re.M):
        return [f"{FILE} needs a '{FINDINGS}' section, even if it says there are none."]
    return []


def count(path: Path) -> int:
    """How many findings the vet's file lists."""

    return len(FINDING.findall(path.read_text(encoding="utf-8"))) if path.is_file() else 0


def vet(runner: Step, agent: config.AgentSpec, run_dir: Path, worktree: Path) -> list[str]:
    """Run the vet step on the agreement; returns what the interview must settle, or
    nothing when the check found nothing or could not run (it says so, and the
    agreement is sealed unchecked rather than held)."""

    folder = runner.run.new_step_dir("vet")
    result = runner(
        "vet", agent, prompt(run_dir, worktree, folder), check=check, folder=folder,
        confined=(worktree, spec.issue_dir(run_dir)),
    )
    if result.outcome != "success":
        runner.run.event("vet.skipped", outcome=result.outcome)
        print(f"\nplacido: the agreement's check ended {result.outcome.replace('-', ' ')};"
              " sealing it unchecked")
        return []
    found = count(folder / FILE)
    runner.run.event("vet.done", findings=found)
    if not found:
        return []
    plural = "1 problem" if found == 1 else f"{found} problems"
    return [SETTLE.format(count=plural, file=folder / FILE)]


def spec_check(
    folder: Path, settings: config.Config, vetter: Callable[[], list[str]]
) -> Callable[[Path], list[str]]:
    """The spec step's check: the agreement's own checks, then, the first time they
    pass, the vet. Once is enough: its findings go to the interview, and what the
    interview settles with the user is the user's to seal."""

    vetted = False

    def check_spec(result: Path) -> list[str]:
        nonlocal vetted
        problems = spec.check(folder, settings)
        if problems or vetted:
            return problems
        vetted = True
        return vetter()

    return check_spec
