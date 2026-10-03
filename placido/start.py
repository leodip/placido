"""`placido start <issue>`: a run folder, a worktree in its own Herdr workspace, and setup."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import TextIO

from placido import __version__, config, doctor, issues, runlog, spec, status
from placido.herdr import Herdr, HerdrError, Worktree
from placido.proc import Runner, run_command


class StartError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise StartError(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def project_root(cwd: Path) -> Path:
    """The main checkout, also from inside one of its worktrees."""

    try:
        common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    except StartError:
        raise StartError(f"{cwd} is not inside a git repository") from None
    return common.parent if common.name == ".git" else Path(git(cwd, "rev-parse", "--show-toplevel"))


def origin_repo(root: Path) -> str | None:
    """owner/name when the repository's origin remote is on GitHub."""

    try:
        return issues.github_repo(git(root, "remote", "get-url", "origin"))
    except StartError:
        return None


def select_run(cwd: Path, runs_root: Path, unready: bool = False) -> Path:
    """The run a command in cwd means. Inside an issue's worktree, that issue's run;
    in the main checkout, the project's only active run. With several issues in
    flight, guessing could act on the wrong one, so placido asks instead. unready
    also counts runs that never got ready, for `placido close`."""

    root = project_root(cwd)
    top = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    runs = runlog.active_runs(runs_root, root.name, str(root), unready)
    if top != root.resolve():
        for run in runs:
            created = runlog.last_event(run, "worktree.created") or {}
            if created.get("path") and Path(created["path"]).resolve() == top:
                return run
        raise StartError(f"no active placido run uses this worktree, {top}")
    if not runs:
        raise StartError(f"no active run for {root.name}; run `placido start <issue>` first")
    if len(runs) > 1:
        lines = []
        for run in runs:
            created = runlog.last_event(run, "worktree.created") or {}
            lines.append(f"  {run.parent.name}: cd {created.get('path', '?')}")
        raise StartError(
            f"{len(runs)} issues are in progress in {root.name}; run placido in the issue's"
            " worktree, or pass --run:\n" + "\n".join(lines)
        )
    return runs[0]


def find_run(root: Path, runs_root: Path, issue: str, unready: bool = False) -> Path:
    """The project's active run for an issue named in full or by a unique prefix,
    such as `02` for 02-many-names. unready as in select_run."""

    runs = runlog.active_runs(runs_root, root.name, str(root), unready)
    found = [run for run in runs if run.parent.name == issue] or [
        run for run in runs if run.parent.name.startswith(issue)
    ]
    if len(found) == 1:
        return found[0]
    names = ", ".join(sorted({run.parent.name for run in runs})) or "none"
    if not found:
        raise StartError(f"no active run of {root.name} matches {issue!r}; active: {names}")
    raise StartError(f"{issue!r} is ambiguous: {', '.join(sorted(r.parent.name for r in found))}")


def branch_name(issue: str) -> str:
    return f"placido/{issue}"


def start(
    arg: str,
    cwd: Path,
    herdr: Herdr | None = None,
    runs_root: Path | None = None,
    echo: TextIO | None = None,
    gh: Runner = run_command,
) -> runlog.Run:
    """Prepare a run for one issue; everything placido does is logged as it happens."""

    herdr = herdr or Herdr()
    root = project_root(cwd)
    settings = config.load(config.find_config(root))
    try:
        found = issues.resolve(arg, root, origin_repo(root), gh)
    except issues.IssueError as error:
        raise StartError(str(error)) from None
    issue = found.id
    branch = branch_name(issue)
    runs_root = runs_root or runlog.runs_dir()
    retry = None
    if git(root, "branch", "--list", branch):
        retry = failed_setup(root, runs_root, issue, branch, settings.base)
        if retry is None:
            raise StartError(
                f"branch {branch} already exists: `placido run` resumes an issue in progress,"
                f" and `placido close {arg}` ends one"
            )
    try:
        base_commit = git(root, "rev-parse", "--verify", f"{settings.base}^{{commit}}")
    except StartError:
        raise StartError(f"base {settings.base!r} is not a commit in {root}") from None

    run = runlog.Run.create(
        runs_root,
        root.name,
        issue,
        {
            "placido": __version__,
            "versions": doctor.tool_versions(),
            "repo": str(root),
            "config": str(settings.source) if settings.source else None,
            "base": settings.base,
            "base_commit": base_commit,
            "branch": branch,
            "issue_title": found.title,
            "issue_source": found.source,
            "issue_number": found.number,
        },
        echo=echo,
    )
    (run.path / "issue.md").write_text(found.text, encoding="utf-8")
    if retry is None:
        run.event("run.start", project=root.name, issue=issue, source=found.source, base=settings.base)
        try:
            worktree = herdr.create_worktree(root, branch, settings.base, issue)
        except HerdrError as error:
            run.event("worktree.failed", error=str(error))
            run.event("run.end", outcome="failed", reason="worktree")
            raise StartError(str(error)) from None
        run.event(
            "worktree.created",
            path=str(worktree.path),
            branch=worktree.branch,
            workspace=worktree.workspace_id,
            pane=worktree.pane_id,
        )
    else:
        worktree = _reuse(run, retry, root, issue, found.source, settings.base)

    _name_repo_workspace(herdr, run, root)
    if settings.setup:
        _status(herdr, run, worktree.workspace_id, status.text(status.WORKING, "setting up"))
        env = run_env(root.name, issue, found.number, worktree.path, run.path)
        code = run_setup(run, settings.setup, worktree.path, env)
        if code != 0:
            _status(herdr, run, worktree.workspace_id, status.text(status.STOPPED, "setup failed"))
            _notify(herdr, run, f"placido · {issue}: setup failed", f"see {run.path / 'setup.log'}")
            run.event("run.end", outcome="failed", reason="setup")
            raise StartError(
                f"setup failed with exit {code}; see {run.path / 'setup.log'}. Fix it, then run"
                f" `placido start {arg}` again to retry setup in the same worktree"
            )
    _status(herdr, run, worktree.workspace_id, status.text(status.READY, "run placido spec"))
    run.event("run.ready", worktree=str(worktree.path))
    # Herdr opens the workspace without moving the user there; once setup is done,
    # switch to it, where the pane is already in the worktree (the user's ask).
    try:
        herdr.focus_workspace(worktree.workspace_id)
        run.event("workspace.focused", workspace=worktree.workspace_id)
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))
    return run


def _name_repo_workspace(herdr: Herdr, run: runlog.Run, root: Path) -> None:
    """Give the repository's workspace, which groups the issues in Herdr's sidebar, the
    repository's name. Otherwise Herdr labels it after its pane's folder, and once that
    pane is in an issue's worktree the group reads as that issue, with the others
    seemingly inside it (2026-10-02). A renamed label stays put."""

    try:
        found = herdr.repo_workspace(root)
        if found and found[1] != root.name:
            herdr.rename_workspace(found[0], root.name)
            run.event("workspace.renamed", workspace=found[0], label=root.name, was=found[1])
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))


def next_steps(run: runlog.Run) -> str:
    """What to do after start: where the issue's worktree is, and the next command."""

    created = runlog.last_event(run.path, "worktree.created") or {}
    following = "placido run" if spec.sealed(spec.issue_dir(run.path)) else "placido spec"
    focused = runlog.last_event(run.path, "workspace.focused")
    where = ("Herdr has switched to the issue's workspace, whose pane is in the worktree."
             if focused else "The issue's workspace in Herdr has a pane in the worktree.")
    return f"{where} Next, there or here:\n  cd {created.get('path', '?')}\n  {following}\n"


def failed_setup(root: Path, runs_root: Path, issue: str, branch: str, base: str) -> Path | None:
    """The issue's latest run when its setup failed and nothing has happened since:
    not closed, its worktree still there, and no commits on the branch beyond the
    base. Starting the issue again retries setup in that worktree."""

    mine = [
        run for run in runlog.all_runs(runs_root)
        if run.parent.parent.name == root.name and run.parent.name == issue
        and runlog.read_record(run).get("repo") in (None, str(root))
    ]
    if not mine:
        return None
    latest = mine[0]
    events = list(runlog.read_events(latest))
    end = runlog.last_event(latest, "run.end") or {}
    created = runlog.last_event(latest, "worktree.created") or {}
    if (
        end.get("reason") != "setup"
        or any(e.get("event") == "run.closed" for e in events)
        or not created.get("path") or not Path(created["path"]).is_dir()
        or git(root, "rev-list", f"{base}..{branch}")
    ):
        return None
    return latest


def _reuse(run: runlog.Run, previous: Path, root: Path, issue: str, source: str, base: str) -> Worktree:
    """Take over the worktree of a run whose setup failed, brought up to the base,
    where a fix to the setup most likely landed."""

    created = runlog.last_event(previous, "worktree.created") or {}
    run.event(
        "run.start", project=root.name, issue=issue, source=source, base=base,
        retry_of=f"{previous.parent.name}/{previous.name}",
    )
    worktree = Worktree(
        path=Path(created["path"]), branch=str(created.get("branch", "")),
        workspace_id=str(created.get("workspace", "")), pane_id=str(created.get("pane", "")),
    )
    done = subprocess.run(
        ["git", "-C", str(worktree.path), "merge", "--ff-only", "-q", base], capture_output=True, text=True
    )
    if done.returncode != 0:
        error = (done.stderr or done.stdout).strip()
        run.event("worktree.failed", error=error)
        run.event("run.end", outcome="failed", reason="worktree")
        raise StartError(f"could not bring {worktree.path} up to {base}: {error}")
    run.event(
        "worktree.created", path=str(worktree.path), branch=worktree.branch,
        workspace=worktree.workspace_id, pane=worktree.pane_id, reused=True,
    )
    return worktree


def run_env(project: str, issue: str, number: int | None, worktree: Path, run_dir: Path) -> dict[str, str]:
    """What project commands and agents are told about the run, as PLACIDO_* variables."""

    name = re.sub(r"[^a-z0-9_-]+", "-", f"{project}-{number or issue}".lower()).strip("-")
    return {
        "PLACIDO_ISSUE": issue,
        "PLACIDO_ISSUE_NUMBER": str(number) if number else "",
        "PLACIDO_NAME": name,
        "PLACIDO_WORKTREE": str(worktree),
        "PLACIDO_RUN_DIR": str(run_dir),
    }


def env_of(run_dir: Path) -> dict[str, str]:
    """The PLACIDO_* variables of an existing run, from its run.json and worktree."""

    record = runlog.read_record(run_dir)
    created = runlog.last_event(run_dir, "worktree.created") or {}
    return run_env(
        record.get("project", ""), record.get("issue", ""), record.get("issue_number"),
        Path(created.get("path", "")), run_dir,
    )


def run_setup(run: runlog.Run, command: str, cwd: Path, env: dict[str, str]) -> int:
    """Run the project's setup command in the worktree, its output in setup.log."""

    return run_project_command(run, "setup", command, cwd, env)


def run_project_command(run: runlog.Run, name: str, command: str, cwd: Path, env: dict[str, str]) -> int:
    """Run one of the project's own commands, such as setup or teardown, in the
    worktree with the run's variables; its output goes to <name>.log."""

    run.event(f"{name}.start", command=command)
    began = time.monotonic()
    with (run.path / f"{name}.log").open("w", encoding="utf-8") as log:
        code = subprocess.run(
            command, shell=True, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **env}
        ).returncode
    run.event(f"{name}.end", exit=code, seconds=round(time.monotonic() - began, 1))
    return code


def _status(herdr: Herdr, run: runlog.Run, workspace_id: str, text: str) -> None:
    status.show(run.path, text)


def _notify(herdr: Herdr, run: runlog.Run, title: str, body: str) -> None:
    try:
        herdr.notify(title, body, sound="request")
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))
