"""Checks on the whole change, and fixing them: the project's final gates, run once
every slice is committed and before the review, and CI on the pull request (step 13).

When a check fails, a fixer agent gets its output, fixes the cause, and placido runs
the checks again and commits, up to MAX_FIXES times; the run stops after that. A fix
never commits by itself, and never weakens a check (skills/checks/SKILL.md).
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from placido import decisions, implement, runlog, spec, status
from placido.step import Step

SKILL = Path(__file__).resolve().parent.parent / "skills" / "checks" / "SKILL.md"
MAX_FIXES = 2


def final_gates(ctx: "driver.Context", fixer: "config.AgentSpec") -> str:  # noqa: F821
    """Run the final gates once, fixing failures; "passed", "none", or "failed".
    A run that passed them already, before an interruption, does not run them again."""

    run, names = ctx.run, list(ctx.settings.final_gates)
    if not names:
        return "none"
    if runlog.last_event(run.path, "final_gates.passed"):
        return "passed"
    for attempt in range(MAX_FIXES + 1):
        folder = run.path / "final-gates" / str(attempt + 1)
        folder.mkdir(parents=True, exist_ok=True)
        status.show(run.path, status.text(status.WORKING, "final gates"))
        problems = implement.run_gates(names, ctx.settings, ctx.worktree, folder, ctx.env, run)
        if not problems:
            run.event("final_gates.passed", attempt=attempt + 1)
            return "passed"
        run.event("final_gates.failed", attempt=attempt + 1, problems=problems)
        if attempt == MAX_FIXES:
            break
        logs = sorted((folder / "gates").glob("*.log"))
        if not fix(ctx, fixer, "the final gates", logs, names, f"final-gates-{attempt + 1}"):
            break
    return "failed"


def fix(
    ctx: "driver.Context", fixer: "config.AgentSpec", what: str, logs: list[Path],  # noqa: F821
    gates: list[str], label: str,
) -> bool:
    """One fixer step on failing check output; True once its fix is committed."""

    head = implement.git(ctx.worktree, "rev-parse", "HEAD")
    entry = {"id": label, "title": f"fix {what}", "gates": gates}
    settings = replace(ctx.settings, mutations=0, slice_gates=())

    def check(result: Path) -> list[str]:
        return implement.check(result, ctx.worktree, head, entry, settings, ctx.env, ctx.run)

    step = ctx.runner("fix", fixer, prompt(ctx, what, logs, gates), name=f"fix-{label}",
                      check=check, ask=implement.blocked)
    if step.outcome != "success":
        ctx.run.event("checks.fix_failed", what=what, outcome=step.outcome)
        return False
    commit(ctx.worktree, step.path / "result.md", ctx.run, label)
    return True


def prompt(ctx: "driver.Context", what: str, logs: list[Path], gates: list[str]) -> str:  # noqa: F821
    issue = spec.issue_dir(ctx.run.path)
    lines = [
        f"# Fix {what}",
        "",
        f"Follow the placido checks skill in {SKILL}.",
        "",
        f"- **What failed:** {what}. Its output:",
        *[f"  - {log}" for log in logs],
        f"- **Agreement:** {issue / 'agreement.md'}",
        decisions.prompt_line(ctx.run.path),
        f"- **Code:** the worktree you are in, {ctx.worktree}. Change it; do not commit.",
        f"- **Checks placido runs after you:** {', '.join(gates) or 'none'}.",
    ]
    from placido import config

    menu = config.command_menu(ctx.settings)
    return "\n".join(lines) + "\n\n" + (menu or "The project defines no commands.\n")


def commit(worktree: Path, result: Path, run: runlog.Run, label: str) -> str:
    text = (
        f"{implement.message(result)}\n\nPlacido-Run: {run.path.parent.name}/{run.path.name}\n"
        f"Placido-Check-Fix: {label}\n"
    )
    implement.git(worktree, "add", "-A")
    done = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-F", "-"], input=text, capture_output=True, text=True
    )
    if done.returncode != 0:
        raise implement.ImplementError(f"git commit: {(done.stderr or done.stdout).strip()}")
    sha = implement.git(worktree, "rev-parse", "HEAD")
    run.event("checks.committed", label=label, commit=sha)
    return sha
