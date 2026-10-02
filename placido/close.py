"""`placido close`: clean up an issue once the user is done with it, usually after
the pull request is merged, or to abandon the issue.

The run's work ends on its own (`run.end`), but its resources stay so the branch can
be tried: the worktree, its Herdr workspace, and whatever the project's setup
started, such as a Docker stack. Closing runs the project's teardown, removes the
worktree and its workspace through Herdr (the branch stays), and logs `run.closed`,
after which the run is no longer active.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from placido import config, driver, implement, runlog, start, summary
from placido.herdr import Herdr, HerdrError


class CloseError(RuntimeError):
    pass


def close(
    run: runlog.Run, herdr: Herdr, settings: config.Config, force: bool = False, here: str | None = None,
) -> None:
    """Close the run. force removes a worktree with uncommitted changes and goes on
    past a failing teardown. here: the calling pane's ID, which must not be in the
    workspace being removed, since removing it would end this very process."""

    created = runlog.last_event(run.path, "worktree.created") or {}
    worktree = Path(created.get("path", ""))
    workspace = str(created.get("workspace", ""))
    if here and workspace and here.split(":")[0] == workspace:
        raise CloseError(
            "this pane belongs to the issue's workspace, which closing removes; run"
            " `placido close` from another workspace, such as the repository's"
        )
    try:
        with driver.Lock(run.path):
            _close(run, herdr, settings, worktree, workspace, force)
    except driver.RunError as error:
        raise CloseError(str(error)) from None


def _close(
    run: runlog.Run, herdr: Herdr, settings: config.Config, worktree: Path, workspace: str, force: bool,
) -> None:
    run.event("close.start", force=force)
    if worktree.is_dir():
        changes = implement.git(worktree, "status", "--porcelain")
        if changes and not force:
            raise CloseError(
                f"{worktree} has uncommitted changes:\n{changes}\n"
                "Commit or stash them, or pass --force to discard them."
            )
        if settings.teardown:
            code = start.run_project_command(run, "teardown", settings.teardown, worktree, start.env_of(run.path))
            if code != 0 and not force:
                raise CloseError(
                    f"teardown failed with exit {code}; see {run.path / 'teardown.log'}. Fix it and"
                    " close again, or pass --force to remove the worktree anyway."
                )
        _remove(run, herdr, worktree, workspace, force)
    else:
        run.event("worktree.missing", path=str(worktree))
    if not runlog.last_event(run.path, "run.end"):
        run.event("run.end", outcome="abandoned", reason="closed before the work was done")
    run.event("run.closed")
    summary.write(run.path)


def _remove(run: runlog.Run, herdr: Herdr, worktree: Path, workspace: str, force: bool) -> None:
    """Remove the checkout through Herdr, which closes its workspace too; with git
    alone when Herdr no longer knows the workspace."""

    try:
        herdr.remove_worktree(workspace, force)
        run.event("worktree.removed", path=str(worktree), workspace=workspace, forced=force)
        return
    except HerdrError as error:
        if error.code == "dirty_worktree_requires_force":
            raise CloseError(f"{worktree} has changes; pass --force to discard them") from None
        run.event("herdr.warning", error=str(error))
    repo = runlog.read_record(run.path).get("repo") or str(worktree)
    done = subprocess.run(
        ["git", "-C", repo, "worktree", "remove", *(["--force"] if force else []), str(worktree)],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise CloseError(f"could not remove {worktree}: {(done.stderr or done.stdout).strip()}")
    run.event("worktree.removed", path=str(worktree), workspace=None, forced=force)
