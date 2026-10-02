"""`placido retro`: an agent looks back on a run and suggests project improvements,
following Matt Pocock's retro: mistakes become automated checks, and judgment calls
become written standards. It only suggests; the user decides what to adopt.

A retro runs on request, since most runs go cleanly. It works on a closed run too:
the agent then reads the project from the repository's main checkout.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from placido import config, decisions, spec
from placido.step import Step, StepResult

SKILL = Path(__file__).resolve().parent.parent / "skills" / "retro" / "SKILL.md"


def prompt(run_dir: Path, code: Path, folder: Path) -> str:
    issue = spec.issue_dir(run_dir)
    notes = code / ".placido" / "notes" / "retro.md"
    lines = [
        f"# Retro on {issue.name}",
        "",
        f"Follow the placido retro skill in {SKILL}.",
        "",
        f"- **The run:** {run_dir} (start with summary.md, then events.jsonl and steps/).",
        f"- **The issue:** {issue} (agreement.md, and {decisions.FILE} if the user decided anything).",
        f"- **The project:** {code}. Read it; change nothing.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        f"- **Write:** {folder / 'retro.md'}.",
    ]
    return "\n".join(lines) + "\n"


def check(result: Path) -> list[str]:
    retro = result.parent / "retro.md"
    if not retro.is_file() or not retro.read_text(encoding="utf-8").strip():
        return [f"write the retro to {retro} first, as the skill shows."]
    if "## Suggestions" not in retro.read_text(encoding="utf-8"):
        return ["retro.md needs a '## Suggestions' section, even if it says there are none."]
    return []


def retro(runner: Step, agent: config.AgentSpec, run_dir: Path, code: Path) -> StepResult:
    """Run the retro step, then keep its retro.md in the run folder too."""

    from placido.step import next_step_number

    folder = run_dir / "steps" / f"{next_step_number(runner.run):02d}-retro"
    result = runner("retro", agent, prompt(run_dir, code, folder), name="retro", check=check)
    if result.outcome == "success":
        shutil.copyfile(result.path / "retro.md", run_dir / "retro.md")
        runner.run.event("retro.done", path=str(run_dir / "retro.md"))
    return result
