"""`placido retro`: an agent looks back on a run and suggests project improvements,
following Matt Pocock's retro: mistakes become automated checks, and judgment calls
become written standards. It only suggests; the user decides what to adopt.

A retro runs on request, since most runs go cleanly. It works on a closed run too:
the agent then reads the project from the repository's main checkout. It can look
back on a project's last few runs at once, where trouble that comes back run after
run shows: that retro is kept in a folder of its own under the project's runs, with
no run.json, so it is never taken for an issue's run.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from placido import config, decisions, runlog, spec
from placido.step import Step, StepResult

SKILL = Path(__file__).resolve().parent.parent / "skills" / "retro" / "SKILL.md"


def prompt(runs: list[Path], code: Path, folder: Path) -> str:
    """The retro's task: one run, or several, newest first, looked back on together."""

    notes = code / ".placido" / "notes" / "retro.md"
    if len(runs) == 1:
        issue = spec.issue_dir(runs[0])
        lines = [
            f"# Retro on {issue.name}",
            "",
            f"Follow the placido retro skill in {SKILL}.",
            "",
            f"- **The run:** {runs[0]} (start with summary.md, then events.jsonl and steps/).",
            f"- **The issue:** {issue} (agreement.md, and {decisions.FILE} if the user decided anything).",
        ]
    else:
        lines = [
            f"# Retro on the last {len(runs)} runs of {runs[0].parent.parent.name}",
            "",
            f"Follow the placido retro skill in {SKILL}; these runs are looked back on together.",
            "",
            "- **The runs**, newest first. In each, start with summary.md, then events.jsonl and"
            f" steps/; its issue folder holds agreement.md, and {decisions.FILE} if the user"
            " decided anything:",
            *(f"  - {run.parent.name}: {run} (issue folder {spec.issue_dir(run)})"
              + (f"; an earlier retro of it: {run / 'retro.md'}" if (run / "retro.md").is_file() else "")
              for run in runs),
        ]
    lines += [
        f"- **The project:** {code}. Read it; change nothing.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        f"- **Write:** {folder / 'retro.md'}.",
    ]
    return "\n".join(lines) + "\n"


def folder(runs_root: Path, project: str, clock: Callable[[], datetime] = runlog.local_now) -> Path:
    """A new folder for a retro on several runs, beside the project's issues."""

    base = runs_root / project / "retros" / runlog.new_run_id(clock())
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.name}-{n}")
    path.mkdir(parents=True)
    return path


def check(result: Path) -> list[str]:
    retro = result.parent / "retro.md"
    if not retro.is_file() or not retro.read_text(encoding="utf-8").strip():
        return [f"write the retro to {retro} first, as the skill shows."]
    if "## Suggestions" not in retro.read_text(encoding="utf-8"):
        return ["retro.md needs a '## Suggestions' section, even if it says there are none."]
    return []


def retro(runner: Step, agent: config.AgentSpec, runs: list[Path], code: Path) -> StepResult:
    """Run the retro step, then keep its retro.md in the runner's run folder too: the
    run's own, or with several runs, the retro's folder."""

    from placido.step import next_step_number

    folder = runner.run.path / "steps" / f"{next_step_number(runner.run):02d}-retro"
    result = runner("retro", agent, prompt(runs, code, folder), name="retro", check=check)
    if result.outcome == "success":
        kept = runner.run.path / "retro.md"
        shutil.copyfile(result.path / "retro.md", kept)
        runner.run.event("retro.done", path=str(kept), runs=[str(run) for run in runs])
    return result
