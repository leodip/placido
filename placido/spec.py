"""The specification step: the interview's prompt, the checks on its output, and the seal."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from placido import config, runlog

SKILL = Path(__file__).resolve().parent.parent / "skills" / "spec" / "SKILL.md"
NOTES = Path(".placido") / "notes" / "spec.md"
SEALED_FILES = ("agreement.md", "slices.json")
# Placido's own copy of the sealed files and the seal, so a step that edits them can be
# put back. Decided 2026-11-03: on #718 an implementer, with the user's leave, edited
# slices.json and rewrote its hash in seal.json, so the seal no longer showed it.
SEALED_COPY = ".sealed"
SECTIONS = ("## Problem", "## Solution", "## Decisions", "## Seams", "## Slices")
# When the issue's goal is a number, the agreement measures it: each implementer reports
# it before and after, the reviewer holds the change to the target, and the pull request
# shows how it moved (decided 2026-10-24).
BENCHMARK = "Benchmark"
BENCHMARK_LINES = ("Command", "Baseline", "Target")
# When the interview settles with the user that nothing can be built until something
# outside the project happens, such as an upstream release, the result's first line
# says when to start, and placido ends the run as deferred: an outcome of its own,
# neither a failure nor work abandoned. Decided 2026-11-21: #494's interview settled
# twice, a month apart, that it waits for golangci-lint 2.16.0. With no way to end, the
# agent left the step waiting, placido emailed that it exited, and `placido close`
# recorded the run as abandoned; the second run measured everything again.
DEFERRED = "Deferred:"
# What a deferral keeps with the run and never posts: the measurements, where they were
# taken, and the routes rejected. Above it, the why is written for the issue's readers,
# as it is posted there (Codex, reviewing 444aead: local paths and hosts went public).
EVIDENCE = "## Evidence"


@dataclass(frozen=True)
class Deferral:
    trigger: str  # when to start, in one line
    why: str  # why it waits, for the issue's readers: what placido posts with the trigger
    evidence: str  # what the interview found and where, for the next run: kept with the run


def section(text: str, name: str, level: int = 2) -> str:
    """A markdown section's text, without its heading."""

    marks = "#" * level
    found = re.search(rf"^{marks} {re.escape(name)}\b[^\n]*\n(.*?)(?=^#{{1,{level}}} |\Z)", text, re.M | re.S)
    return found.group(1).strip() if found else ""


def benchmark(folder: Path) -> str:
    """The agreement's Benchmark section, or "" when the issue's goal is not a number."""

    agreement = folder / "agreement.md"
    return section(agreement.read_text(encoding="utf-8"), BENCHMARK) if agreement.is_file() else ""


def issue_dir(run_dir: Path) -> Path:
    """Where an issue's agreement lives, shared by every run of the issue."""

    return run_dir.parent


def prompt(run_dir: Path, worktree: Path, settings: config.Config) -> str:
    """The task for the spec agent: the skill to follow and this run's paths."""

    record = runlog.read_record(run_dir)
    folder = issue_dir(run_dir)
    notes = worktree / NOTES
    lines = [
        f"# Specify {record['issue']}",
        "",
        f"Follow the placido spec skill in {SKILL}.",
        "",
        f"- **Issue:** {run_dir / 'issue.md'} (from {record.get('issue_source', 'unknown')})",
        f"- **Code:** the worktree you are in, {worktree}, on branch {record.get('branch')}."
        " Do not change it.",
        f"- **Write:** {folder / 'agreement.md'} and {folder / 'slices.json'};"
        f" probes go in {folder / 'probe'}.",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        f"- **Slice gates:** every slice runs {', '.join(settings.slice_gates) or 'no gates'};"
        " give a slice extra gates only from the commands below, each a command's name with"
        " the arguments its description asks for, if any, after it (\"data mysql\").",
        f"- **Nothing to build yet:** when the user decides the issue waits for something outside"
        f" the project, write no agreement: write the result as `{DEFERRED} <when to start>`, then"
        f" why for the issue's readers, then `{EVIDENCE}`, as the skill's section on deferring says."
        " Placido ends the run as deferred"
        + (f" and posts what is above `{EVIDENCE}` on the issue." if record.get("issue_number") else "."),
    ]
    for run, event in deferred_before(run_dir):
        lines.append(
            f"- **Deferred before:** run {run.name} ended deferred: {event.get('trigger')}. Its reasons"
            f" are in {event.get('result')}. Check first whether that has come to pass, and build on what"
            " it found rather than measure it all again."
        )
    menu = config.command_menu(settings)
    return "\n".join(lines) + "\n\n" + (menu or "The project defines no commands.\n")


def deferral(result: Path) -> Deferral | None:
    """The deferral a spec result states, or None when it states none."""

    text = result.read_text(encoding="utf-8").strip() if result.is_file() else ""
    if not text.startswith(DEFERRED):
        return None
    first, _, rest = text.partition("\n")
    parts = re.split(rf"(?m)^{re.escape(EVIDENCE)}[ \t]*$", rest, maxsplit=1)
    why, evidence = parts[0], parts[1] if len(parts) > 1 else ""
    return Deferral(first[len(DEFERRED):].strip(), why.strip(), evidence.strip())


def deferral_problems(found: Deferral) -> list[str]:
    """What a deferral lacks, worded for the agent to fix."""

    problems = []
    if not found.trigger:
        problems.append(f"the result's first line needs when to start after '{DEFERRED}', in one line,"
                        f" such as '{DEFERRED} start once <the release> is out'.")
    if not found.why:
        problems.append(f"the deferral needs why after its first line and before '{EVIDENCE}', for the"
                        " issue's readers, as placido posts it there.")
    if not found.evidence:
        problems.append(f"the deferral needs a '{EVIDENCE}' section: what you measured and checked, and"
                        " where, for the next run; it stays with the run.")
    return problems


def deferred_before(run_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """The issue's other runs whose spec deferred it, oldest first, each with that event."""

    runs = sorted(p for p in issue_dir(run_dir).iterdir() if p != run_dir and (p / "run.json").is_file())
    found = []
    for run in runs:
        event = runlog.last_event(run, "spec.deferred")
        if event is not None:
            found.append((run, event))
    return found


def check(folder: Path, settings: config.Config) -> list[str]:
    """Problems with the agreement and slice list, worded for the agent to fix."""

    problems: list[str] = []
    agreement = folder / "agreement.md"
    text = agreement.read_text(encoding="utf-8") if agreement.is_file() else ""
    if not text.strip():
        problems.append(f"{agreement} is missing or empty.")
    else:
        for heading in SECTIONS:
            if heading not in text:
                problems.append(f"agreement.md has no '{heading}' section.")
        if re.search(rf"^## {BENCHMARK}\b", text, re.M):
            measured = section(text, BENCHMARK)
            for name in BENCHMARK_LINES:
                if not re.search(rf"^- \*\*{name}:\*\*[ \t]*\S", measured, re.M):
                    problems.append(f"agreement.md's {BENCHMARK} section has no '- **{name}:**' line.")
    slices, error = _slices(folder / "slices.json")
    if error:
        return problems + [error]
    plain = _plain(text)
    for n, entry in enumerate(slices, 1):
        problems += _check_slice(n, entry, settings)
        title = entry.get("title") if isinstance(entry, dict) else None
        if text and isinstance(title, str) and title.strip() and _plain(title) not in plain:
            problems.append(f"slice {n}'s title {title!r} is not in agreement.md's Slices section.")
    return problems


def _plain(text: str) -> str:
    """Text without markdown emphasis or code marks, so formatting alone is no mismatch."""

    return " ".join(re.sub(r"[*_`]", "", text).split())


def _slices(path: Path) -> tuple[list[Any], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], f"{path} is missing."
    except ValueError as error:
        return [], f"slices.json is not valid JSON: {error}."
    slices = data.get("slices") if isinstance(data, dict) else None
    if not isinstance(slices, list) or not slices:
        return [], 'slices.json needs a non-empty "slices" list.'
    return slices, ""


def _check_slice(n: int, entry: Any, settings: config.Config) -> list[str]:
    where = f"slice {n} in slices.json"
    if not isinstance(entry, dict):
        return [f"{where} is not an object."]
    problems = []
    if entry.get("id") != n:
        problems.append(f"{where} has id {entry.get('id')!r}; ids count up from 1, so it should be {n}.")
    for key in ("title", "delivers"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            problems.append(f"{where} needs a non-empty {key!r}.")
    blocked = entry.get("blocked_by")
    if not isinstance(blocked, list) or not all(isinstance(b, int) for b in blocked):
        problems.append(f"{where} needs 'blocked_by' as a list of slice ids.")
    else:
        for b in blocked:
            if not 1 <= b < n:
                problems.append(f"{where} is blocked by {b}; a slice can only be blocked by an earlier one.")
    gates = entry.get("gates")
    if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
        problems.append(f"{where} needs 'gates' as a list of command names.")
    else:
        for gate in gates:
            bad = config.unknown_command(gate, settings.commands)
            if bad is not None:
                known = ", ".join(settings.commands) or "none"
                which = "which" if bad == gate else f"whose command {bad!r}"
                problems.append(f"{where} has gate {gate!r}, {which} is not a project command (commands: {known}).")
    return problems


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def seal(folder: Path, run: runlog.Run) -> dict[str, Any]:
    """Seal the agreement: record when, by which run, and the hash of each file."""

    record = {
        "sealed_at": run.clock().isoformat(timespec="seconds"),
        "run": run.path.name,
        "files": {name: _digest(folder / name) for name in SEALED_FILES},
    }
    (folder / "seal.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    copy = folder / SEALED_COPY
    copy.mkdir(exist_ok=True)
    for name in (*SEALED_FILES, "seal.json"):
        shutil.copyfile(folder / name, copy / name)
    run.event("spec.sealed", **record["files"])
    return record


def restore(folder: Path, run: runlog.Run) -> list[str]:
    """Put back each sealed file, or the seal, that differs from placido's copy; returns
    their names. An agreement sealed before placido kept a copy is left as it is."""

    copy = folder / SEALED_COPY
    restored = []
    for name in (*SEALED_FILES, "seal.json"):
        kept = copy / name
        if not kept.is_file():
            continue
        here = folder / name
        if here.is_file() and here.read_bytes() == kept.read_bytes():
            continue
        shutil.copyfile(kept, here)
        restored.append(name)
    if restored:
        run.event("seal.restored", files=restored)
    return restored


def sealed(folder: Path) -> bool:
    """Whether the agreement is sealed and unchanged since."""

    try:
        record = json.loads((folder / "seal.json").read_text(encoding="utf-8"))
        return all(
            record["files"][name] == _digest(folder / name) for name in SEALED_FILES
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def load_slices(folder: Path) -> list[dict[str, Any]]:
    return json.loads((folder / "slices.json").read_text(encoding="utf-8"))["slices"]
