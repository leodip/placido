"""Checks on the whole change, and fixing them: the project's final gates, run once
every slice is committed and before the review; its last gates, run once the review
is done and before the push, for checks too slow or costly to repeat; and CI on the
pull request (step 13).

When a check fails, a fixer agent gets its output and fixes the cause. Placido runs
the checks again on the fix before it commits it: for gates, that run is the proof,
and they do not run a third time; CI gets up to MAX_FIXES fixes. When a fix cannot
pass, a failing final gate stops the run; failing last gates or CI leave the pull
request flagged. A fix never commits by itself, and never weakens a check
(skills/checks/SKILL.md). A fixer that finds the cause outside the change, such as a
test the base branch fails too, changes nothing and says why, and the check stays
failing with that reason (decided 2026-10-23: #489's fixer could end only with a
change, and asked the user twice while main's own e2e tests failed).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from placido import decisions, gate, implement, runlog, spec, status
from placido.step import Step

SKILL = Path(__file__).resolve().parent.parent / "skills" / "checks" / "SKILL.md"
MAX_FIXES = 2
NO_CHANGES = "No code changes"  # a fixer's first line when nothing in the change causes the failure
FOLLOW_UP = "## Follow-up"  # in a No code changes result: a fix outside the change, to draft as an issue
NOTES = Path(".placido") / "notes" / "fix.md"


def final_gates(ctx: "driver.Context", fixer: "config.AgentSpec") -> str:  # noqa: F821
    """Run the final gates once, fixing failures; "passed", "none", or "failed".
    A run that passed them already, before an interruption, does not run them again."""

    if runlog.last_event(ctx.run.path, "final_gates.passed"):
        return "passed"
    return _gates(ctx, fixer, "final", list(ctx.settings.final_gates))


def last_gates(ctx: "driver.Context", fixer: "config.AgentSpec") -> str:  # noqa: F821
    """Run the last gates once, fixing failures; "passed", "none", or "failed". A run
    that has their outcome already, before an interruption, does not run them again:
    a failure is not retried, since the pull request opens flagged with it."""

    if runlog.last_event(ctx.run.path, "last_gates.passed"):
        return "passed"
    if runlog.last_event(ctx.run.path, "last_gates.gave_up"):
        return "failed"
    return _gates(ctx, fixer, "last", list(ctx.settings.last_gates))


def _gates(ctx: "driver.Context", fixer: "config.AgentSpec", kind: str, names: list[str]) -> str:  # noqa: F821
    run = ctx.run
    if not names:
        return "none"
    folder = run.path / f"{kind}-gates" / "1"
    folder.mkdir(parents=True, exist_ok=True)
    status.show(run.path, status.text(status.WORKING, f"{kind} gates"))
    # Each gate's passing log, attempt by attempt, for the tests it names flaky (flaky()).
    mark = _seen(run)
    problems = implement.run_gates(names, ctx.settings, ctx.worktree, folder, ctx.env, run)
    passing = _passing(run, mark, names)
    if not problems:
        run.event(f"{kind}_gates.passed", attempt=1, **flaky(passing, names))
        return "passed"
    run.event(f"{kind}_gates.failed", attempt=1, problems=problems)
    logs = sorted((folder / "gates").glob("*.log"))
    mark = _seen(run)
    if fix(ctx, fixer, f"the {kind} gates", logs, names, f"{kind}-gates-1"):
        # The fix's check ran these same gates on the tree placido then committed, and
        # they passed: running them again would repeat it, and a slow gate (a suite
        # against a deployment) took ten more minutes for nothing (#580, 2026-10-14).
        run.event(f"{kind}_gates.passed", attempt=2, by="fix", **flaky(_passing(run, mark, names), names))
        return "passed"
    failed = [n for n in names if any(p.startswith(f"gate {n} (`") for p in problems)]
    # Nothing fixed: the failed gates run once more before placido gives up, as a flaky
    # test may pass then; #869's e2e was left failing on one try (2026-11-20). The others
    # keep their first run's logs: a fix that was not taken ran them on another tree.
    again = run.path / f"{kind}-gates" / "2"
    again.mkdir(parents=True, exist_ok=True)
    status.show(run.path, status.text(status.WORKING, f"{kind} gates, once more"))
    mark = _seen(run)
    problems = implement.run_gates(failed or names, ctx.settings, ctx.worktree, again, ctx.env, run)
    passing.update(_passing(run, mark, failed or names))
    if not problems:
        run.event(f"{kind}_gates.passed", attempt=2, by="rerun", **flaky(passing, names))
        return "passed"
    run.event(f"{kind}_gates.failed", attempt=2, problems=problems)
    failed = [n for n in names if any(p.startswith(f"gate {n} (`") for p in problems)]
    # The pull request opens flagged, and the gates that passed still say their flaky tests.
    held = {n: log for n, log in passing.items() if n not in failed}
    run.event(f"{kind}_gates.gave_up", gates=failed, **flaky(held, names))
    return "failed"


def _seen(run: runlog.Run) -> int:
    """How many events the run has: the mark from which _passing reads an attempt's."""

    return sum(1 for _ in runlog.read_events(run.path))


def _passing(run: runlog.Run, mark: int, names: list[str]) -> dict[str, str]:
    """The log of each of the gates that passed, run or reused, in the events after mark:
    those of one attempt."""

    logs: dict[str, str] = {}
    for n, event in enumerate(runlog.read_events(run.path)):
        passed = (event.get("event") == "gate.end" and event.get("exit") == 0) or event.get("event") == "gate.reused"
        if n >= mark and passed and event.get("gate") in names and event.get("log"):
            logs[event["gate"]] = str(event["log"])
    return logs


def flaky(passing: dict[str, str], names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The tests each passing gate's log says passed only when it ran them again
    (gate.FLAKY), up to gate.FLAKY_KEPT a gate, as the `flaky` field of the stage's own
    passed or gave_up event: placido shows a passing gate's log to nobody, so the pull
    request and the summary say them. Kept with the outcome, so a pass interrupted
    before it was recorded leaves none behind. Empty when no log says any."""

    notes = []
    for name in names:
        tests = gate.flaky(Path(passing[name])) if name in passing else None
        if tests is not None:
            notes.append({"gate": name, "tests": tests[:gate.FLAKY_KEPT], "count": len(tests), "log": passing[name]})
    return {"flaky": notes} if notes else {}


def fix(
    ctx: "driver.Context", fixer: "config.AgentSpec", what: str, logs: list[Path],  # noqa: F821
    gates: list[str], label: str,
) -> bool:
    """One fixer step on failing check output; True once its fix is committed."""

    head = implement.git(ctx.worktree, "rev-parse", "HEAD")
    entry = {"id": label, "title": f"fix {what}", "gates": gates}
    settings = replace(ctx.settings, mutations=0, slice_gates=())

    def check(result: Path) -> list[str]:
        if unchanged(result) is None:
            return implement.check(result, ctx.worktree, head, entry, settings, ctx.env, ctx.run)
        # Nothing to run again: the checks failed on this very tree.
        problems = decisions.check(result.parent, ctx.run.path) + check_follow_up(result)
        if implement.git(ctx.worktree, "rev-parse", "HEAD") != head or implement.git(ctx.worktree, "status", "--porcelain"):
            problems.append(f"the result says '{NO_CHANGES}', but the worktree changed: put it back as it was"
                            f" (`git reset --hard {head}`), or write the result of a fix.")
        return problems

    step = ctx.runner("fix", fixer, prompt(ctx, what, logs, gates), name=f"fix-{label}",
                      check=check, ask=implement.blocked)
    if step.outcome != "success":
        ctx.run.event("checks.fix_failed", what=what, outcome=step.outcome)
        return False
    why = unchanged(step.path / "result.md")
    if why is not None:
        found = follow_up(step.path / "result.md")
        ctx.run.event("checks.unchanged", what=what, why=why, label=label, **({"followup": found} if found else {}))
        return False
    commit(ctx.worktree, step.path / "result.md", ctx.run, label)
    return True


def unchanged(result: Path) -> str | None:
    """Why the fixer changed nothing, when its result says it did not: what it wrote
    above the evidence, in one paragraph, whole; else None. The summary and the pull
    request quote it, and a cut at 500 characters left #420's and #492's mid-word."""

    text = result.read_text(encoding="utf-8").strip() if result.is_file() else ""
    if not text.startswith(NO_CHANGES):
        return None
    why = " ".join(re.split(r"(?m)^## ", text[len(NO_CHANGES):], maxsplit=1)[0].split()).lstrip(":.;, ")
    return why or "the fixer gave no reason"


def follow_up(result: Path) -> dict[str, str] | None:
    """The fix a No code changes result recommends outside the change, as a follow-up
    to draft: its title, the section's first line, and what to fix, the rest. #869's
    fixer named the fix for a test that took ten random letters for a link, and it went
    nowhere but the run's log (2026-11-20)."""

    text = result.read_text(encoding="utf-8") if result.is_file() else ""
    found = re.search(rf"(?ms)^{re.escape(FOLLOW_UP)}[ \t]*$(.*?)(?=^## |\Z)", text)
    if not found:
        return None
    lines = found.group(1).strip().splitlines()
    title = lines[0].strip().lstrip("#").strip() if lines else ""
    description = "\n".join(lines[1:]).strip()
    return {"title": title, "description": description} if title and description else None


def check_follow_up(result: Path) -> list[str]:
    text = result.read_text(encoding="utf-8") if result.is_file() else ""
    if re.search(rf"(?m)^{re.escape(FOLLOW_UP)}[ \t]*$", text) and follow_up(result) is None:
        return [f"the '{FOLLOW_UP}' section needs a title on its first line, then what to fix and where."]
    return []


def outside(run_dir: Path, what: str) -> str | None:
    """The fixer's reason for changing nothing on what failed, if it did."""

    found = [e for e in runlog.read_events(run_dir) if e.get("event") == "checks.unchanged" and e.get("what") == what]
    return found[-1].get("why") if found else None


def prompt(ctx: "driver.Context", what: str, logs: list[Path], gates: list[str]) -> str:  # noqa: F821
    issue = spec.issue_dir(ctx.run.path)
    notes = ctx.worktree / NOTES
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
        implement.subject_line(),
        f"- **Checks placido runs after you:** {', '.join(gates) or 'none'}. A gate you run with"
        " `placido gate <gate>` and that passes is not run again on the same files.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
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
