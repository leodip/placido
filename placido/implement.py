"""Implementing one slice: which slice, the implementer's prompt, the checks, the commit."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from placido import config, decisions, mutate, runlog, spec

SKILL = Path(__file__).resolve().parent.parent / "skills" / "implement" / "SKILL.md"
NOTES = Path(".placido") / "notes" / "implement.md"
EVIDENCE = "## Evidence"
BLOCKED = "Blocked:"


class ImplementError(RuntimeError):
    pass


def committed(run_dir: Path) -> set[int]:
    """Slices this run has committed."""

    return {
        event["slice"] for event in runlog.read_events(run_dir)
        if event.get("event") == "slice.committed" and isinstance(event.get("slice"), int)
    }


def choose(slices: list[dict[str, Any]], done: set[int], wanted: int | None = None) -> dict[str, Any]:
    """The slice to build: the one asked for, or the first whose blockers are all done."""

    by_id = {s["id"]: s for s in slices}
    if wanted is not None:
        if wanted not in by_id:
            raise ImplementError(f"there is no slice {wanted}; the agreement has {len(slices)}")
        if wanted in done:
            raise ImplementError(f"slice {wanted} is already committed")
        missing = [b for b in by_id[wanted]["blocked_by"] if b not in done]
        if missing:
            raise ImplementError(f"slice {wanted} is blocked by {', '.join(map(str, missing))}, not yet committed")
        return by_id[wanted]
    for entry in slices:
        if entry["id"] not in done and all(b in done for b in entry["blocked_by"]):
            return entry
    raise ImplementError("every slice is committed" if len(done) >= len(slices) else "no slice is ready")


def slice_results(run_dir: Path) -> list[Path]:
    """The result of each committed slice, in commit order: what it built, and the
    follow-ups its implementer noted."""

    steps = run_dir / "steps"
    found = []
    for event in runlog.read_events(run_dir):
        if event.get("event") != "slice.committed":
            continue
        folders = sorted(steps.glob(f"*-implement-slice-{event.get('slice')}")) if steps.is_dir() else []
        result = next((f / "result.md" for f in reversed(folders) if (f / "result.md").is_file()), None)
        if result is not None:
            found.append(result)
    return found


def gates_for(entry: dict[str, Any], settings: config.Config) -> list[str]:
    names = list(settings.slice_gates)
    names += [g for g in entry.get("gates", []) if g not in names]
    return names


def prompt(run_dir: Path, worktree: Path, entry: dict[str, Any], settings: config.Config) -> str:
    folder = spec.issue_dir(run_dir)
    notes = worktree / NOTES
    gates = gates_for(entry, settings)
    lines = [
        f"# Implement slice {entry['id']}: {entry['title']}",
        "",
        f"Follow the placido implement skill in {SKILL}.",
        "",
        f"- **Agreement:** {folder / 'agreement.md'} (sealed; read it in full)",
        f"- **This slice:** {entry['id']}, \"{entry['title']}\": {entry['delivers']}",
        f"- **Code:** the worktree you are in, {worktree}. Change it; do not commit.",
        f"- **Tests first:** {'yes' if settings.test_first else 'no, write them after the code'}.",
        f"- **Gates for this slice:** {', '.join(gates) or 'none'}. Placido runs them after you,"
        " and commits only when they pass.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        decisions.prompt_line(run_dir),
    ]
    if settings.mutations:
        lines.append(
            f"- **Mutations:** at least {settings.mutations} killed, aimed at the riskiest behavior of"
            " this slice, with `placido mutate <file> --replace \"<old>\" --with \"<new>\" --"
            " <narrow test command>`."
        )
    menu = config.command_menu(settings)
    return "\n".join(lines) + "\n\n" + (menu or "The project defines no commands.\n")


def git(worktree: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise ImplementError(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def blocked(result: Path) -> str | None:
    """The question of a blocked result, or None."""

    text = result.read_text(encoding="utf-8").strip() if result.is_file() else ""
    first = text.splitlines()[0] if text else ""
    return first[len(BLOCKED):].strip() if first.startswith(BLOCKED) else None


def message(result: Path) -> str:
    """The commit message: everything in the result above the evidence."""

    text = result.read_text(encoding="utf-8")
    return text.split(EVIDENCE, 1)[0].strip()


def run_gates(
    names: list[str], settings: config.Config, worktree: Path, step_dir: Path,
    env: dict[str, str], run: runlog.Run,
) -> list[str]:
    """Run each gate in the worktree, its output in gates/<name>.log; problems for failures."""

    folder = step_dir / "gates"
    folder.mkdir(exist_ok=True)
    problems = []
    for name in names:
        command = settings.commands[name].run
        run.event("gate.start", gate=name, command=command)
        began = time.monotonic()
        log = folder / f"{name}.log"
        with log.open("w", encoding="utf-8") as out:
            code = subprocess.run(
                command, shell=True, cwd=worktree, stdout=out, stderr=subprocess.STDOUT,
                env={**os.environ, **env},
            ).returncode
        run.event("gate.end", gate=name, exit=code, seconds=round(time.monotonic() - began, 1))
        if code != 0:
            problems.append(f"gate {name} (`{command}`) failed with exit {code}; its output is in {log}.")
    return problems


def check(
    result: Path, worktree: Path, head: str, entry: dict[str, Any], settings: config.Config,
    env: dict[str, str], run: runlog.Run,
) -> list[str]:
    """Problems with the slice, cheapest first, so the gates run only on a plausible result."""

    if blocked(result) is not None:
        return []
    problems = decisions.check(result.parent, run.path)
    subject = message(result).splitlines()[0].strip() if message(result) else ""
    if not subject:
        problems.append("the result has no commit subject on its first line.")
    elif len(subject) > 72:
        problems.append(f"the commit subject is {len(subject)} characters; keep it to 72.")
    if EVIDENCE not in result.read_text(encoding="utf-8"):
        problems.append(f"the result has no '{EVIDENCE}' section.")
    if git(worktree, "rev-parse", "HEAD") != head:
        problems.append(
            f"you committed, but placido commits the slice. Undo it with `git reset --soft {head}`,"
            " keeping your changes."
        )
    if not git(worktree, "status", "--porcelain"):
        problems.append("the worktree has no changes.")
    if settings.mutations:
        records = mutate.read(result.parent)
        # Distinct mutations only: running the same one twice proves nothing new.
        killed = len({
            (r["file"], r["replace"], r["with"]) for r in records if r["outcome"] == mutate.KILLED
        })
        if killed < settings.mutations:
            problems.append(
                f"{killed} mutation{'s' if killed != 1 else ''} killed; the slice needs"
                f" {settings.mutations}. Use `placido mutate` as your prompt shows."
            )
    if problems:
        return problems
    return run_gates(gates_for(entry, settings), settings, worktree, result.parent, env, run)


def commit(worktree: Path, result: Path, entry: dict[str, Any], run: runlog.Run) -> str:
    """Commit everything in the worktree as the slice; returns the commit's hash."""

    text = (
        f"{message(result)}\n\nPlacido-Run: {run.path.parent.name}/{run.path.name}\n"
        f"Placido-Slice: {entry['id']}\n"
    )
    git(worktree, "add", "-A")
    done = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-F", "-"], input=text, capture_output=True, text=True
    )
    if done.returncode != 0:
        raise ImplementError(f"git commit: {(done.stderr or done.stdout).strip()}")
    sha = git(worktree, "rev-parse", "HEAD")
    run.event("slice.committed", slice=entry["id"], title=entry["title"], commit=sha)
    return sha
