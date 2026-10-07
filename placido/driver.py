"""`placido run`: build every slice in blocker order, and resume after an interruption.

A run's state is its event log: a slice is done when it has a `slice.committed`
event, and an attempt that started without ending was interrupted. So a run killed
at any moment resumes by reading its own log, with nothing else to keep in sync.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from placido import alerts, config, fallback, implement, runlog, spec, status
from placido.herdr import Herdr, HerdrError
from placido.step import Step

ENDED = ("slice.committed", "slice.failed", "slice.blocked")  # slice.blocked: logs from before step 10
# Failed attempts that say nothing about the slice's work, so they do not count
# against stage_attempts: the user's Ctrl+C, and a refusal or quota that moves the
# role to its next agent.
NOT_COUNTED = ("interrupted", *fallback.SWITCH)
MAX_CONTEXT_FAILURES = 2  # a slice that overflows a fresh session twice is too big


class RunError(RuntimeError):
    pass


@dataclass
class Context:
    """Everything one slice attempt needs."""

    run: runlog.Run
    runner: Step
    herdr: Herdr
    settings: config.Config
    agent: config.AgentSpec
    worktree: Path
    env: dict[str, str]
    chain: fallback.Chain | None = None  # the implement role's agents; made by drive()


def build_slice(ctx: Context, entry: dict[str, Any], total: int | None = None) -> str:
    """One attempt at one slice: committed, or the step's failed outcome. A question
    for the user is answered in the agent's tab while the step waits."""

    run = ctx.run
    head = implement.git(ctx.worktree, "rev-parse", "HEAD")
    of = {"of": total} if total else {}  # how many slices in all, so the log shows how far along
    run.event("slice.start", slice=entry["id"], **of, title=entry["title"], head=head)
    result = ctx.runner(
        "implement", ctx.agent, implement.prompt(run.path, ctx.worktree, entry, ctx.settings),
        name=f"implement-slice-{entry['id']}",
        check=lambda result: implement.check(
            result, ctx.worktree, head, entry, ctx.settings, ctx.env, run
        ),
        ask=implement.blocked,
    )
    if result.outcome != "success":
        run.event("slice.failed", slice=entry["id"], outcome=result.outcome)
        return result.outcome
    try:
        implement.commit(ctx.worktree, result.path / "result.md", entry, run)
    except implement.ImplementError as error:
        run.event("slice.failed", slice=entry["id"], outcome="commit-failed", error=str(error))
        return "commit-failed"
    return "committed"


def interrupted(run_dir: Path) -> dict[str, Any] | None:
    """The last slice attempt if it started and never ended, with its last agent."""

    attempt: dict[str, Any] | None = None
    for event in runlog.read_events(run_dir):
        kind = event.get("event")
        if kind == "slice.start":
            attempt = {"slice": event.get("slice"), "head": event.get("head"), "agent": None}
        elif kind in ENDED:
            attempt = None
        elif kind == "agent.start" and attempt is not None:
            attempt["agent"] = event
    return attempt


def recover(ctx: Context, slices: list[dict[str, Any]]) -> None:
    """Tidy up after an interrupted attempt: stop its agent, keeping its transcript.
    If the agent had finished and its result passes every check, commit it rather than
    redo the work; otherwise set its changes aside in a git stash and start again."""

    attempt = interrupted(ctx.run.path)
    if attempt is None:
        return
    number = attempt["slice"]
    started = attempt["agent"]
    if started:
        folder = ctx.run.path / "steps" / started["step"]
        ctx.runner.close_agent(started["name"], started["pane"], folder, started["agent"])
        entry = next((s for s in slices if s["id"] == number), None)
        if entry is not None and _salvage(ctx, entry, folder / "result.md", attempt.get("head")):
            return
    stash = _stash(ctx, f"placido: interrupted slice {number} ({ctx.run.path.name})")
    ctx.run.event("slice.failed", slice=number, outcome="interrupted", stash=stash)


def _salvage(ctx: Context, entry: dict[str, Any], result: Path, head: str | None) -> bool:
    """Commit an interrupted attempt that had finished, when its result passes the checks."""

    if not result.is_file() or implement.blocked(result) is not None:
        return False
    head = head or implement.git(ctx.worktree, "rev-parse", "HEAD")
    problems = implement.check(result, ctx.worktree, head, entry, ctx.settings, ctx.env, ctx.run)
    if problems:
        ctx.run.event("slice.unsalvaged", slice=entry["id"], problems=problems)
        return False
    implement.commit(ctx.worktree, result, entry, ctx.run)
    ctx.run.event("slice.salvaged", slice=entry["id"])
    return True


def _stash(ctx: Context, message: str) -> str | None:
    """Set aside the worktree's changes, returning the stash message, or None if clean."""

    if not implement.git(ctx.worktree, "status", "--porcelain"):
        return None
    implement.git(ctx.worktree, "stash", "push", "--include-untracked", "-m", message)
    ctx.run.event("worktree.stashed", message=message)
    return message


def stopping(ctx: Context, slices: list[dict[str, Any]]) -> None:
    """After Ctrl+C: stop the slice's agent now rather than leave it working until the
    next resume, so it spends nothing on work that is set aside anyway."""

    _status(ctx, status.text(status.STOPPED, "interrupted"))
    if interrupted(ctx.run.path) is None:
        return
    print("\nplacido: stopping the agent and setting its changes aside…", flush=True)
    try:
        recover(ctx, slices)
    except KeyboardInterrupt:
        pass  # a second Ctrl+C: leave the rest to the next resume


def failures(run_dir: Path, number: int, outcome: str | None = None) -> int:
    """Failed attempts at a slice, or only those with this outcome. An interruption is
    the user's choice, and a refusal or quota is the agent's limit, not a failure."""

    return sum(
        1 for event in runlog.read_events(run_dir)
        if event.get("event") == "slice.failed" and event.get("slice") == number
        and event.get("outcome") not in NOT_COUNTED
        and (outcome is None or event.get("outcome") == outcome)
    )


def _failure_message(run_dir: Path) -> str:
    failed = runlog.last_event(run_dir, "agent.failed") or {}
    return str(failed.get("message", ""))


def implement_chain(ctx: Context) -> fallback.Chain:
    """The implement role's chain, positioned where the run's log left it, so every
    command that builds slices continues on the agent the run had moved to."""

    if ctx.chain is None:
        ctx.chain = fallback.Chain(
            ctx.run, "implement", fallback.entries(ctx.settings, "implement", ctx.agent),
            lambda title, body: _notify(ctx, title, body),
        )
    return ctx.chain


def after_failure(ctx: Context, number: int, outcome: str) -> str | None:
    """Set a failed attempt's changes aside and answer its failure: move to the next
    agent after a refusal or quota, stop when the agent is logged out or the slice
    keeps overflowing the context. Returns how the run must stop, or None to go on."""

    run = ctx.run
    _stash(ctx, f"placido: slice {number} attempt failed ({outcome}, {run.path.name})")
    if outcome in fallback.SWITCH:
        # The partial work was set aside: the next agent builds the slice afresh.
        if not implement_chain(ctx).switch(outcome, _failure_message(run.path), step=f"slice {number}"):
            return {"refusal": "refused", "quota": "out-of-quota"}.get(outcome, "unavailable")
        ctx.agent = ctx.chain.current
    elif outcome == "auth":
        _notify(ctx, f"{ctx.agent.agent} is logged out",
                f"{_failure_message(run.path)[:160]} · log in, then run placido again")
        return "logged-out"
    elif outcome == "context" and failures(run.path, number, "context") >= MAX_CONTEXT_FAILURES:
        run.event("slice.gave_up", slice=number, reason="context")
        _notify(ctx, f"slice {number} overflows the context window",
                "it overflowed a fresh session twice; it may need splitting")
        return "gave-up"
    return None


def drive(ctx: Context, slices: list[dict[str, Any]]) -> str:
    """Build slices until all are committed or one cannot be: it failed too often, every
    agent refused it, or its agent is logged out."""

    run = ctx.run
    ctx.agent = implement_chain(ctx).current  # a resumed run stays on the agent it had moved to
    recover(ctx, slices)
    while True:
        done = implement.committed(run.path)
        if len(done) == len(slices):
            run.event("run.slices_done", slices=len(slices))
            _status(ctx, status.text(status.WORKING, f"{len(slices)} slices built"))
            return "done"
        try:
            entry = implement.choose(slices, done)
        except implement.ImplementError as error:
            raise RunError(str(error)) from None
        number = entry["id"]
        if failures(run.path, number) >= ctx.settings.stage_attempts:
            run.event("slice.gave_up", slice=number, attempts=ctx.settings.stage_attempts)
            _notify(ctx, f"slice {number} failed {ctx.settings.stage_attempts} times",
                    "see the run log; `placido implement` can retry it by hand")
            return "gave-up"
        _status(ctx, status.text(status.WORKING, f"slice {number}/{len(slices)}"))
        outcome = build_slice(ctx, entry, len(slices))
        if outcome == "committed":
            continue
        stop = after_failure(ctx, number, outcome)
        if stop:
            return stop


# How a stopped run's status reads.
STOPPED = {
    "gave-up": "a slice gave up",
    "refused": "every agent refused",
    "out-of-quota": "out of quota",
    "unavailable": "no agent can run",
    "logged-out": "agent logged out",
    "final-gates": "final gates fail",
}


def show_stopped(ctx: Context, outcome: str) -> None:
    _status(ctx, status.text(status.STOPPED, STOPPED.get(outcome, outcome)))


def show(ctx: Context, state: str, detail: str) -> None:
    _status(ctx, status.text(state, detail))


def _status(ctx: Context, text: str) -> None:
    status.show(ctx.run.path, text)


def notify(ctx: Context, title: str, body: str, times: bool = False) -> None:
    _notify(ctx, title, body, times)


def _notify(ctx: Context, title: str, body: str, times: bool = False) -> None:
    alerts.notify(ctx.run, ctx.herdr, title, body, times=times)


class Lock:
    """One placido process per run: a second `placido run` on the same run refuses."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "lock"

    def __enter__(self) -> "Lock":
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self._holder()
            if pid and _alive(pid):
                raise RunError(f"placido (pid {pid}) is already working on this run") from None
            self.path.unlink(missing_ok=True)  # left by a process that died
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as out:
            out.write(f"{os.getpid()}\n")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)

    def _holder(self) -> int | None:
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
