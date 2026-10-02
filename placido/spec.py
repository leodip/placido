"""The specification step: the interview's prompt, the checks on its output, and the seal."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from placido import config, runlog

SKILL = Path(__file__).resolve().parent.parent / "skills" / "spec" / "SKILL.md"
NOTES = Path(".placido") / "notes" / "spec.md"
SEALED_FILES = ("agreement.md", "slices.json")
SECTIONS = ("## Problem", "## Solution", "## Decisions", "## Seams", "## Slices")


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
        " give a slice extra gates only from the commands below.",
    ]
    menu = config.command_menu(settings)
    return "\n".join(lines) + "\n\n" + (menu or "The project defines no commands.\n")


def check(folder: Path, settings: config.Config) -> list[str]:
    """Problems with the agreement and slice list, worded for the agent to fix."""

    problems: list[str] = []
    agreement = folder / "agreement.md"
    text = agreement.read_text(encoding="utf-8") if agreement.is_file() else ""
    if not text.strip():
        problems.append(f"{agreement} is missing or empty.")
    else:
        for section in SECTIONS:
            if section not in text:
                problems.append(f"agreement.md has no '{section}' section.")
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
            if gate not in settings.commands:
                known = ", ".join(settings.commands) or "none"
                problems.append(f"{where} has gate {gate!r}, which is not a project command (commands: {known}).")
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
    run.event("spec.sealed", **record["files"])
    return record


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
