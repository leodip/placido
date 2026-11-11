"""`placido gate`: run one of a step's gates and record a pass against the exact files
it ran on, so placido does not run it again on the same files.

An implementer runs its slice's gates before it finishes, and placido then ran them
all again on the same tree: on #813 slice 2 that was the data tier on four engines
twice, about 28 minutes for nothing (decided 2026-11-11). A gate's pass now goes into
the run's gates.jsonl with a fingerprint of the worktree: the tree git would commit,
untracked files included and ignored ones left out. Before it runs a gate, placido
looks for a pass of the same gate and command on the same fingerprint, its own or an
agent's, and reuses it. Any change to the files since, by the agent or anyone, makes
the fingerprint differ and the gate runs. A pass during which the files changed is not
recorded, since it proves nothing about either state.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from placido import config, proc

RECORD = "gates.jsonl"  # in the run folder, shared by every step of the run
TAIL = 30  # lines of the gate's output shown to the agent


def fingerprint(worktree: Path) -> str | None:
    """The tree git would commit from the worktree as it is, untracked files included;
    None when git cannot tell. Built in a copy of the index, so the real one is untouched."""

    def git(*args: str, env: dict[str, str] | None = None) -> str | None:
        done = subprocess.run(
            ["git", "-C", str(worktree), *args], capture_output=True, text=True,
            env={**os.environ, **(env or {})},
        )
        return done.stdout.strip() if done.returncode == 0 else None

    index = git("rev-parse", "--path-format=absolute", "--git-path", "index")
    if not index:
        return None
    with tempfile.TemporaryDirectory(prefix="placido-gate-") as scratch:
        copy = Path(scratch) / "index"
        if Path(index).is_file():
            shutil.copyfile(index, copy)  # keeps the stat cache, so unchanged files are not hashed again
        env = {"GIT_INDEX_FILE": str(copy)}
        if git("add", "-A", env=env) is None:
            return None
        return git("write-tree", env=env)


def record(
    run_dir: Path, gate: str, command: str, tree: str, log: Path, by: str, seconds: float,
) -> None:
    """Note that gate passed on tree."""

    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "gate": gate, "command": command, "tree": tree, "exit": 0,
        "seconds": round(seconds, 1), "log": str(log), "by": by,
    }
    with (run_dir / RECORD).open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry) + "\n")


def passed(run_dir: Path, gate: str, command: str, tree: str | None) -> dict[str, Any] | None:
    """The latest pass of the gate, running command, on tree, or None."""

    path = run_dir / RECORD
    if tree is None or not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("exit") == 0 \
                and (entry.get("gate"), entry.get("command"), entry.get("tree")) == (gate, command, tree):
            found = entry
    return found


def run(
    gate: str, command: str, worktree: Path, env: dict[str, str], log: Path, run_dir: Path, by: str,
    still: Any = lambda seconds, line: None,
) -> tuple[int, float]:
    """Run the gate's command, its output in log, and record a pass made on files that did
    not change while it ran; its exit code and seconds."""

    before = fingerprint(worktree)
    began = time.monotonic()
    code = proc.run_shell(command, worktree, env, log, still)
    seconds = time.monotonic() - began
    if code == 0 and before is not None and fingerprint(worktree) == before:
        record(run_dir, gate, command, before, log, by, seconds)
    return code, seconds


def main(words: list[str]) -> int:
    """The CLI: the gate's exit code, or 2 on a usage error."""

    gate = " ".join(words).strip()
    step_dir, run_dir = os.environ.get("PLACIDO_STEP_DIR"), os.environ.get("PLACIDO_RUN_DIR")
    if step_dir and not run_dir and Path(step_dir).parent.name == "steps":
        # A step started before steps were given PLACIDO_RUN_DIR: its run holds its steps.
        run_dir = str(Path(step_dir).parent.parent)
    if not gate:
        print("placido gate: name the gate, as your prompt lists it: placido gate data mysql", flush=True)
        return 2
    if not step_dir or not run_dir:
        print("placido gate: run it from a placido step, which sets PLACIDO_STEP_DIR and PLACIDO_RUN_DIR",
              flush=True)
        return 2
    repo = os.environ.get("PLACIDO_REPO")
    try:
        settings = config.load(config.find_config(Path(repo) if repo else Path.cwd()))
    except config.ConfigError as error:
        print(f"placido gate: {error}", flush=True)
        return 2
    unknown = config.unknown_command(gate, settings.commands)
    if unknown is not None:
        print(f"placido gate: {unknown!r} is not a project command ({', '.join(settings.commands) or 'none'})",
              flush=True)
        return 2
    worktree = Path(os.environ.get("PLACIDO_WORKTREE") or Path.cwd())
    command = config.gate_command(gate, settings.commands)
    folder = Path(step_dir) / "gates"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"{config.gate_log(gate)}.log"
    code, seconds = run(gate, command, worktree, dict(os.environ), log, Path(run_dir), "agent")
    lines = log.read_text(encoding="utf-8", errors="replace").rstrip().splitlines()
    print("\n".join(lines[-TAIL:]))
    if code == 0:
        print(f"\ngate {gate} passed in {config.format_duration(round(seconds))}; its output is in {log}."
              " Placido will not run it again on these exact files.")
    else:
        print(f"\ngate {gate} FAILED with exit {code}; its output is in {log}.")
    sys.stdout.flush()
    return code
