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

A `placido pr` run reviewed someone else's pull request: its branch goes once the pull
request is merged or closed and every commit made on it here reached it, and before
its teardown runs from the worktree, the host paths are put back as they were trusted,
since the pull request's tests could write to them (decided 2026-10-16).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from placido import codexd, config, driver, pr, runlog, start, summary
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


def _reviewed_outcome(run: runlog.Run) -> tuple[str, str]:
    """How a `placido pr` review ended, for its run.end: it never ends by itself, since
    what happens to the pull request is the user's."""

    kinds = {event.get("event") for event in runlog.read_events(run.path)}
    if "pr.merged" in kinds:
        return "merged", "the pull request was merged from placido"
    if "pr.closed_pr" in kinds:
        return "closed", "the pull request was closed from placido"
    if "pr.ready" in kinds:
        return "reviewed", "the review was done; what became of the pull request is the user's"
    return "abandoned", "closed before the review was done"


def _close_lanes(run: runlog.Run, settings: config.Config) -> None:
    """Tear down the lane each extra reviewer worked in, then remove its worktree, which
    holds nothing of placido's. A failing teardown is logged and the worktree goes anyway."""

    repo = runlog.read_record(run.path).get("repo")
    for lane in [e for e in runlog.read_events(run.path) if e.get("event") == "pr.lane"]:
        path = Path(lane["path"])
        if not path.is_dir():
            continue
        _restore_host_paths(run, path, settings, primary=False)
        if settings.teardown:
            env = {**start.env_of(run.path), "PLACIDO_NAME": lane["name"], "PLACIDO_WORKTREE": str(path)}
            start.run_project_command(run, f"teardown-{lane['letter'].lower()}", settings.teardown, path, env)
        done = _git(Path(repo or path), "worktree", "remove", "--force", str(path))
        if done.returncode == 0:
            run.event("lane.removed", letter=lane["letter"], path=str(path))
        else:
            run.event("lane.remove_failed", letter=lane["letter"], error=(done.stderr or done.stdout).strip()[-300:])


def _restore_host_paths(run: runlog.Run, worktree: Path, settings: config.Config, primary: bool = True) -> set[str]:
    """Put the host paths back as the review last trusted them, so the teardown that runs
    from them is the project's, whatever the pull request's tests wrote, and return the
    files put back. A lane never has the base merged in, so its reference is the round's
    own."""

    paths = settings.pr.host_paths
    rounds = pr.rounds(run.path)
    if not paths or not rounds:
        return set()
    reference = pr.trusted(run.path, rounds[-1], primary)
    changed = set(_git(worktree, "diff", "--name-only", reference, "--", *paths).stdout.splitlines())
    loose = _git(worktree, "status", "--porcelain", "--untracked-files=all", "--", *paths).stdout
    changed |= {line[3:] for line in loose.splitlines()}
    for name in sorted(changed):
        if _git(worktree, "cat-file", "-e", f"{reference}:{name}").returncode == 0:
            _git(worktree, "checkout", reference, "--", name)
        else:
            (worktree / name).unlink(missing_ok=True)
            _git(worktree, "rm", "-q", "--cached", "--ignore-unmatch", "--", name)
    if changed:
        run.event("close.host_paths_restored", files=sorted(changed), reference=reference)
    return changed


def _merged(run: runlog.Run, repo: Path, base: str, branch: str, gh: Runner) -> str | None:
    """Why the branch counts as merged, or None: its pull request merged, or the
    branch already in the base."""

    record = runlog.read_record(run.path)
    if record.get("kind") == "pr":
        return _reviewed(repo, branch, record, gh)
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


def _reviewed(repo: Path, branch: str, record: dict, gh: Runner) -> str | None:
    """Why a reviewed pull request's branch can go, or None: the pull request merged or
    closed, and every commit made on the branch here on it."""

    number, owner_repo = record.get("pr_number"), start.origin_repo(repo)
    if not number or not owner_repo:
        return None
    result = gh(["gh", "pr", "view", str(number), "-R", owner_repo, "--json", "state", "--jq", ".state"], timeout=60)
    state = result.out.strip() if result.code == 0 else ""
    if state not in ("MERGED", "CLOSED"):
        return None
    ref = f"refs/remotes/origin/pr/{number}"
    _git(repo, "fetch", "-q", "origin", f"+refs/pull/{number}/head:{ref}")
    if _git(repo, "merge-base", "--is-ancestor", branch, ref).returncode != 0:
        return None  # it holds commits made here that never reached the pull request
    _git(repo, "update-ref", "-d", ref)
    return f"pull request #{number} {state.lower()}"


def _close(
    run: runlog.Run, herdr: Herdr, settings: config.Config, worktree: Path, workspace: str, force: bool,
) -> None:
    run.event("close.start", force=force)
    reviewed = runlog.read_record(run.path).get("kind") == "pr"
    if reviewed:
        _close_lanes(run, settings)
    if worktree.is_dir():
        restored = _restore_host_paths(run, worktree, settings) if reviewed else set()
        # The files the restore put back differ from the branch by placido's doing, not the
        # user's, as when a commit made by hand changed a host path placido never trusted.
        changes = "\n".join(line for line in _git(worktree, "status", "--porcelain").stdout.splitlines()
                            if line[3:] not in restored)
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
        # The restored files leave the checkout dirty, and the check above found nothing else.
        _remove(run, herdr, worktree, workspace, force or bool(restored))
    else:
        run.event("worktree.missing", path=str(worktree))
    if not runlog.last_event(run.path, "run.end"):
        outcome, reason = _reviewed_outcome(run) if reviewed else ("abandoned", "closed before the work was done")
        run.event("run.end", outcome=outcome, reason=reason)
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
