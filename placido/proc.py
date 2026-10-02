"""Run an outside command and capture its result, without ever raising."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable


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
