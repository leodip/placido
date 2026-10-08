"""`placido close`: clean up an issue once the user is done with it, usually after
the pull request is merged, or to abandon the issue.

The run's work ends on its own (`run.end`), but its resources stay so the branch can
be tried: the worktree, its Herdr workspace, and whatever the project's setup
started, such as a Docker stack. Closing runs the project's teardown, removes the
worktree and its workspace through Herdr, and logs `run.closed`, after which the run
is no longer active.

Then it tidies the main checkout, as the user did by hand after the first real run
(decided 2026-10-02): it brings the base branch up to date (a fast-forward, only
when the checkout is on the base with no changes), and deletes the issue's branch
once its work is merged: its pull request merged on GitHub, which a squash merge
hides from git, or the branch merged into the base, and drops the git stashes the run
set aside. An unmerged branch and its stashes stay.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from placido import codexd, config, driver, implement, runlog, start, summary
from placido.herdr import Herdr, HerdrError
from placido.proc import Runner, run_command


class CloseError(RuntimeError):
    pass


def close(
    run: runlog.Run, herdr: Herdr, settings: config.Config, force: bool = False, here: str | None = None,
    gh: Runner = run_command,
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
    try:
        _tidy(run, gh)
    except (OSError, subprocess.SubprocessError) as error:
        run.event("close.tidy_failed", error=str(error))


def _tidy(run: runlog.Run, gh: Runner) -> None:
    """Bring the base up to date in the main checkout, then delete the issue's branch
    if its work is merged."""

    record = runlog.read_record(run.path)
    repo, base, branch = record.get("repo"), record.get("base") or "main", record.get("branch")
    if not repo or not branch or not Path(repo).is_dir():
        return
    _pull(run, Path(repo), base)
    merged = _merged(run, Path(repo), base, branch, gh)
    if not _git(Path(repo), "branch", "--list", branch).stdout.strip():
        return
    if merged is None:
        run.event("branch.kept", branch=branch, reason="not merged")
        return
    done = _git(Path(repo), "branch", "-D", branch)
    if done.returncode == 0:
        run.event("branch.deleted", branch=branch, reason=merged)
        _drop_stashes(run, Path(repo))
    else:
        run.event("branch.kept", branch=branch, reason=(done.stderr or done.stdout).strip())


def _drop_stashes(run: runlog.Run, repo: Path) -> None:
    """Drop the stashes this run set aside (interrupted or failed attempts), now that
    its work is merged: stashes belong to the whole repository and would pile up."""

    listed = _git(repo, "stash", "list", "--format=%gd%x09%gs").stdout.splitlines()
    mine = [line.split("\t", 1) for line in listed if "\t" in line]
    mine = [(ref, message) for ref, message in mine
            if "placido: " in message and f"{run.path.name})" in message]
    for ref, message in reversed(mine):  # highest index first, so the others keep theirs
        if _git(repo, "stash", "drop", "-q", ref).returncode == 0:
            run.event("stash.dropped", message=message.split(": ", 1)[-1])


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _pull(run: runlog.Run, repo: Path, base: str) -> None:
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != base:
        run.event("base.pull_skipped", reason=f"the checkout is on {current}, not {base}")
        return
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        run.event("base.pull_skipped", reason="the checkout has uncommitted changes")
        return
    if not _git(repo, "remote").stdout.strip():
        run.event("base.pull_skipped", reason="no remote")
        return
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    done = _git(repo, "pull", "--ff-only", "-q")
    if done.returncode != 0:
        run.event("base.pull_skipped", reason=(done.stderr or done.stdout).strip()[-300:])
        return
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    run.event("base.pulled", base=base, before=before[:12], after=after[:12])


def _merged(run: runlog.Run, repo: Path, base: str, branch: str, gh: Runner) -> str | None:
    """Why the branch counts as merged, or None: its pull request merged, or the
    branch already in the base."""

    opened = runlog.last_event(run.path, "pr.opened")
    owner_repo = start.origin_repo(repo)
    if opened and owner_repo:
        result = gh(["gh", "pr", "view", str(opened.get("number")), "-R", owner_repo,
                     "--json", "state", "--jq", ".state"], timeout=60)
        if result.code == 0 and result.out.strip() == "MERGED":
            return f"pull request #{opened.get('number')} merged"
    if _git(repo, "merge-base", "--is-ancestor", branch, base).returncode == 0:
        return f"merged into {base}"
    return None


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
        codexd.before_removing(run, worktree)
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
