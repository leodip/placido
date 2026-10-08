"""Keep Codex's shared daemon out of the worktrees placido deletes.

Codex runs one app-server daemon for every session, started by the first `codex`
that finds none, and the daemon keeps that session's folder as its own. When the
folder is an issue's worktree, `placido close` deletes it from under the daemon, and
from then on every new Codex session fails to start ("Experimental feature request
failed", openai/codex#50619; #500's worktree, 2026-10-08). So placido starts the
daemon from the home folder before it launches Codex, moves it there before closing
the worktree it runs from, and `placido doctor` reports one whose folder is gone.

Restarting the daemon cuts off the Codex sessions on it, so placido restarts it
only when no other Codex session is running, and otherwise says how to do it later.
Linux only: the daemon is found through /proc, and elsewhere nothing is found.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from placido import runlog
from placido.proc import Result

DELETED = " (deleted)"
FIX = "cd ~ && codex app-server daemon restart"


def _run(argv: list[str], cwd: Path) -> Result:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return Result(1, str(error))
    return Result(done.returncode, (done.stdout + done.stderr).strip())


RUNNER: list[Callable[[list[str], Path], Result]] = [_run]  # the test suite replaces it
PROC = [Path("/proc")]  # likewise


@dataclass(frozen=True)
class Daemon:
    pid: int
    folder: Path  # where it runs from
    gone: bool  # that folder was deleted

    def inside(self, folder: Path) -> bool:
        return self.folder == folder or folder in self.folder.parents


def _processes() -> list[tuple[int, list[str], str]]:
    """Each process's ID, arguments, and folder, as far as they can be read."""

    found = []
    try:
        entries = list(PROC[0].iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            folder = os.readlink(entry / "cwd")
        except OSError:
            continue
        found.append((int(entry.name), [a.decode(errors="replace") for a in args if a], folder))
    return found


def daemons() -> list[Daemon]:
    """The running Codex app-server daemons; normally one."""

    found = []
    for pid, args, folder in _processes():
        if "app-server" in args and "--managed-daemon" in args:
            gone = folder.endswith(DELETED)
            path = Path(folder[: -len(DELETED)] if gone else folder)
            found.append(Daemon(pid, path, gone or not path.is_dir()))
    return found


def sessions() -> list[int]:
    """The Codex sessions running, whose daemon a restart would cut off."""

    mine = os.getpid()
    return [
        pid for pid, args, _ in _processes()
        if pid != mine and "app-server" not in args
        and any(Path(a).name in ("codex", "codex.js") for a in args[:2])
    ]


def _daemon(command: str) -> Result:
    return RUNNER[0](["codex", "app-server", "daemon", command], Path.home())


def ensure(run: runlog.Run) -> None:
    """Before placido launches Codex: a daemon running from the home folder. Starting
    one does nothing when one runs; one whose folder is gone is restarted from home,
    unless a Codex session would be cut off."""

    found = daemons()
    if not found:
        result = _daemon("start")
        if result.code != 0:
            run.event("codex.daemon_warning", error=result.out[-300:])
        return
    gone = next((d for d in found if d.gone), None)
    if gone is not None:
        _restart(run, gone, "its folder is gone")


def before_removing(run: runlog.Run, worktree: Path) -> None:
    """Before `placido close` removes the worktree: move a daemon running from it to
    the home folder."""

    worktree = worktree.resolve()
    daemon = next((d for d in daemons() if d.inside(worktree)), None)
    if daemon is not None:
        _restart(run, daemon, "it runs from the worktree being removed")


def _restart(run: runlog.Run, daemon: Daemon, why: str) -> None:
    running = sessions()
    if running:
        run.event("codex.daemon_kept", folder=str(daemon.folder), reason=why, sessions=len(running), fix=FIX)
        return
    result = _daemon("restart")
    if result.code != 0:
        run.event("codex.daemon_warning", folder=str(daemon.folder), error=result.out[-300:], fix=FIX)
        return
    run.event("codex.daemon_restarted", was=str(daemon.folder), reason=why)


def problem() -> str | None:
    """What is wrong with the daemon, for `placido doctor`, or None."""

    gone = next((d for d in daemons() if d.gone), None)
    if gone is None:
        return None
    return f"its folder is gone ({gone.folder}), so new sessions fail; once no Codex session runs: {FIX}"
