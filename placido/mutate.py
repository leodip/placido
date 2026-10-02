"""`placido mutate`: break one piece of code on purpose and check the tests notice.

A mutation is killed when the tests fail with it, which proves they guard that
piece of code; it survives when they still pass, which points at a missing or weak
test. The file is restored byte for byte afterwards, whatever happens, because the
agent's uncommitted work lives in it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

KILLED, SURVIVED, TIMEOUT = "killed", "survived", "timeout"


class MutateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mutation:
    outcome: str
    exit: int | None
    seconds: float
    output: str


def mutate(path: Path, old: str, new: str, command: list[str], timeout: float = 900) -> Mutation:
    """Replace old with new in path, run command, and restore path exactly."""

    original = path.read_bytes()
    text = original.decode("utf-8")
    count = text.count(old)
    if count != 1:
        where = "not found" if count == 0 else f"found {count} times"
        raise MutateError(f"the text to replace is {where} in {path}; it must match exactly once")
    if old == new:
        raise MutateError("the replacement is the same as the original text")

    def restore(*_: Any) -> None:
        path.write_bytes(original)

    previous = {sig: signal.signal(sig, _restore_and_exit(restore)) for sig in (signal.SIGINT, signal.SIGTERM)}
    began = time.monotonic()
    try:
        path.write_bytes(text.replace(old, new).encode("utf-8"))
        shell = len(command) == 1
        try:
            done = subprocess.run(
                command[0] if shell else command, shell=shell, capture_output=True, text=True,
                timeout=timeout,
            )
            outcome = KILLED if done.returncode != 0 else SURVIVED
            code, output = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired as expired:
            outcome, code = TIMEOUT, None
            output = f"{_text(expired.stdout)}{_text(expired.stderr)}\n(timed out after {timeout:g}s)\n"
    finally:
        restore()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if path.read_bytes() != original:
        raise MutateError(f"{path} could not be restored; check it by hand")
    return Mutation(outcome, code, round(time.monotonic() - began, 1), output)


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _restore_and_exit(restore: Any) -> Any:
    def handler(signum: int, frame: Any) -> None:
        restore()
        raise SystemExit(128 + signum)

    return handler


def record(step_dir: Path, path: Path, old: str, new: str, command: list[str], result: Mutation) -> Path:
    """Append the mutation to the step's mutations.jsonl and save its output."""

    folder = step_dir / "mutations"
    folder.mkdir(parents=True, exist_ok=True)
    number = len(list(folder.glob("*.log"))) + 1
    log = folder / f"{number:02d}.log"
    log.write_text(result.output, encoding="utf-8")
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "file": str(path),
        "replace": old,
        "with": new,
        "command": " ".join(command),
        "outcome": result.outcome,
        "exit": result.exit,
        "seconds": result.seconds,
        "log": log.name,
    }
    with (step_dir / "mutations.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log


def read(step_dir: Path) -> list[dict[str, Any]]:
    path = step_dir / "mutations.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(file: str, old: str, new: str, command: list[str], timeout: float) -> int:
    """The CLI: 0 when killed, 1 when it survived, 3 on timeout, 2 on a usage error."""

    if not command:
        print("placido mutate: give the test command after --", flush=True)
        return 2
    path = Path(file)
    try:
        result = mutate(path, old, new, command, timeout)
    except (MutateError, OSError, UnicodeDecodeError) as error:
        print(f"placido mutate: {error}", flush=True)
        return 2
    step_dir = os.environ.get("PLACIDO_STEP_DIR")
    log = record(Path(step_dir), path.resolve(), old, new, command, result) if step_dir else None
    tail = "\n".join(result.output.rstrip().splitlines()[-15:])
    messages = {
        KILLED: f"killed: the tests failed with the mutation (exit {result.exit}), so they guard this code.",
        SURVIVED: "SURVIVED: the tests still pass with the code broken. Add or strengthen a test, "
        "or explain why the mutation does not change behavior.",
        TIMEOUT: "timeout: the tests did not finish; the mutation proves nothing either way.",
    }
    print(f"{messages[result.outcome]}\n{path} is restored.")
    if log:
        print(f"recorded in {log.parent.parent / 'mutations.jsonl'}")
    print(f"--- last lines of the test output ---\n{tail}")
    return {KILLED: 0, SURVIVED: 1, TIMEOUT: 3}[result.outcome]
