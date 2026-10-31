"""Run an outside command and capture its result, without ever raising; or run one of
the project's own, its output in a log, saying all along that it still runs."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# How often a long project command, such as a gate or setup, says it still runs: an
# agent's step does, so a quiet screen read as stuck (decided 2026-10-24).
PROGRESS_SECONDS = 30

# How much of a command's last line the progress line carries, and the terminal codes
# and controls taken out of it first.
SAID_CHARS = 100
_CONTROLS = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Result:
    code: int
    out: str


Runner = Callable[[list[str]], Result]


def run_command(argv: list[str], timeout: float | None = 30) -> Result:
    """Run a command and return its exit code and combined output; never raises."""

    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return Result(127, f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(124, f"{' '.join(argv)}: timed out after {timeout:g}s")
    except OSError as error:
        return Result(126, f"{argv[0]}: {error}")
    return Result(done.returncode, (done.stdout + done.stderr).strip())


def last_line(log: Path) -> str:
    """The last line a command wrote to its log, plain and short; "" when none. It says
    what a long command is doing, such as waiting on a lock another run holds."""

    try:
        with log.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.replace("\r", "\n").splitlines()):
        line = " ".join(_CONTROLS.sub("", line).split())
        if line:
            return line if len(line) <= SAID_CHARS else line[: SAID_CHARS - 1] + "…"
    return ""


def run_shell(
    command: str, cwd: Path, env: dict[str, str], log: Path, still: Callable[[float, str], None],
) -> int:
    """Run a shell command, its output in log, calling still(seconds so far, its last
    line) every PROGRESS_SECONDS while it runs; its exit code."""

    began = time.monotonic()
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env={**os.environ, **env},
        )
        try:
            while True:
                try:
                    return proc.wait(timeout=PROGRESS_SECONDS)
                except subprocess.TimeoutExpired:
                    still(time.monotonic() - began, last_line(log))
        except BaseException:  # as subprocess.run: an interrupted placido leaves no command behind
            proc.kill()
            proc.wait()
            raise
