"""Implementing one slice: which slice, the implementer's prompt, the checks, the commit."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from placido import config, decisions, gate, mutate, runlog, spec

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


def gates_for(entry: dict[str, Any], settings: config.Config, run_dir: Path | None = None) -> list[str]:
    """The project's slice gates, then the slice's own: the agreement's, or those the
    latest decision about the slice set, when run_dir is given."""

    extra = entry.get("gates", [])
    decided = decisions.gates(run_dir, entry["id"]) if run_dir is not None and "id" in entry else None
    if decided is not None and not decided.problem:
        extra = decided.gates
    names = list(settings.slice_gates)
    names += [g for g in extra if g not in names]
    return names


def decided_gates_problems(entry: dict[str, Any], settings: config.Config, run_dir: Path) -> list[str]:
    """What is wrong with the gates a decision set for the slice, if one did."""

    decided = decisions.gates(run_dir, entry["id"])
    if decided is None:
        return []
    if decided.problem:
        return [decided.problem]
    problems = []
    for gate in decided.gates:
        bad = config.unknown_command(gate, settings.commands)
        if bad is not None:
            known = ", ".join(settings.commands) or "none"
            problems.append(
                f"decision D{decided.decision} sets gate {gate!r}, which is not a project command"
                f" (commands: {known})."
            )
    return problems


def prompt(run_dir: Path, worktree: Path, entry: dict[str, Any], settings: config.Config) -> str:
    folder = spec.issue_dir(run_dir)
    notes = worktree / NOTES
    gates = gates_for(entry, settings, run_dir)
    lines = [
        f"# Implement slice {entry['id']}: {entry['title']}",
        "",
        f"Follow the placido implement skill in {SKILL}.",
        "",
        f"- **Agreement:** {folder / 'agreement.md'} (sealed; read it in full)",
        f"- **This slice:** {entry['id']}, \"{entry['title']}\": {entry['delivers']}",
        f"- **Code:** the worktree you are in, {worktree}. Change it; do not commit.",
        f"- **Tests first:** {'yes' if settings.test_first else 'no, write them after the code'}.",
        f"- **Gates for this slice:** {', '.join(gates) or 'none'}. Each is a command below, with any"
        " arguments after its name. Run each with `placido gate <gate>` once your work is done:"
        " placido commits only when they all pass, and runs again only those whose files changed"
        " since they passed.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        decisions.prompt_line(run_dir),
    ]
    if spec.benchmark(folder):
        lines.append(
            f"- **Benchmark:** the agreement's {spec.BENCHMARK} section. When this slice can move the"
            " number, measure it with its command before you change anything and again at the end,"
            " and report both on a `- **Benchmark:** <before> → <after>` line of your evidence."
        )
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
    """Run each gate in the worktree, its output in gates/<gate>.log; problems for failures.
    A gate that already passed on these exact files, run by an agent through `placido
    gate` or by placido, is not run again (placido/gate.py)."""

    folder = step_dir / "gates"
    folder.mkdir(exist_ok=True)
    problems = []
    for name in names:
        command = config.gate_command(name, settings.commands)
        log = folder / f"{config.gate_log(name)}.log"
        earlier = gate.passed(run.path, name, command, gate.fingerprint(worktree))
        if earlier is not None:
            if Path(earlier["log"]) != log:
                log.write_text(f"Not run again: it passed on these exact files at {earlier['ts']},"
                               f" run by {earlier['by']}; its output is in {earlier['log']}.\n", encoding="utf-8")
            run.event("gate.reused", gate=name, command=command, by=earlier["by"], ran=earlier["ts"],
                      seconds_saved=earlier["seconds"], log=earlier["log"])
            continue
        run.event("gate.start", gate=name, command=command)
        code, seconds = gate.run(
            name, command, worktree, env, log, run.path, "placido",
            lambda s, said, n=name: run.progress(f"gate {n} running", s, said),
        )
        run.event("gate.end", gate=name, exit=code, seconds=round(seconds, 1))
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
    restored = spec.restore(spec.issue_dir(run.path), run)
    problems = [
        f"you changed the sealed {', '.join(restored)}; placido put back the sealed version. Leave"
        " the agreement as sealed: record what the user decided in the decisions, which win over"
        " it, and a decision about this slice's gates with a **Gates:** line."
    ] if restored else []
    problems += decisions.check(result.parent, run.path)
    problems += decided_gates_problems(entry, settings, run.path)
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
    decided = decisions.gates(run.path, entry["id"])
    if decided is not None:
        run.event("slice.gates_decided", slice=entry["id"], decision=decided.decision, gates=decided.gates)
    return run_gates(gates_for(entry, settings, run.path), settings, worktree, result.parent, env, run)


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
