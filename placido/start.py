"""`placido start <issue>`: a run folder, a worktree in its own Herdr workspace, and setup."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TextIO

from placido import __version__, alerts, config, doctor, driver, issues, runlog, spec, status
from placido.herdr import Herdr, HerdrError, Worktree
from placido import proc
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
    hand_off: bool = False,
) -> runlog.Run:
    """Prepare a run for one issue; everything placido does is logged as it happens.
    With hand_off, typed outside the issue's worktree, only the run and its worktree
    are made here: setup and the interview go on in the issue's own workspace."""

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
    waiting = handed_off(root, runs_root, issue)
    if waiting is not None:
        return _go_on(runlog.Run(waiting, echo=echo), herdr, settings, root, found.number, arg, cwd, hand_off)
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
    created = runlog.last_event(run.path, "worktree.created") or {}
    if hand_off and hand_over(run, herdr, created, f"placido start {shlex.quote(arg)} && {SPEC}"):
        return run
    _set_up(run, herdr, settings, root, found.number, worktree, arg, launch=True)
    return run


def _go_on(
    run: runlog.Run, herdr: Herdr, settings: config.Config, root: Path, number: int | None, arg: str,
    cwd: Path, hand_off: bool,
) -> runlog.Run:
    """Go on with a run handed to its workspace before its setup finished: there, set it
    up; from anywhere else, switch to it, or hand it over again if nothing works on it."""

    created = runlog.last_event(run.path, "worktree.created") or {}
    there = stays(created, cwd)
    if hand_off and not there and hand_over(run, herdr, created, f"placido start {shlex.quote(arg)} && {SPEC}"):
        return run
    worktree = Worktree(
        path=Path(created["path"]), branch=str(created.get("branch", "")),
        workspace_id=str(created.get("workspace", "")), pane_id=str(created.get("pane", "")),
    )
    try:
        with driver.Lock(run.path):
            # In the issue's own pane the interview follows the handed-off command itself.
            _set_up(run, herdr, settings, root, number, worktree, arg, launch=not there)
    except driver.RunError as error:
        raise StartError(str(error)) from None
    return run


def _set_up(
    run: runlog.Run, herdr: Herdr, settings: config.Config, root: Path, number: int | None,
    worktree: Worktree, arg: str, launch: bool,
) -> None:
    """Run the project's setup in the worktree; then, with launch, switch to the issue's
    workspace and start the interview in its pane."""

    issue = run.path.parent.name
    if settings.setup:
        _status(herdr, run, worktree.workspace_id, status.text(status.WORKING, "setting up"))
        env = run_env(root.name, issue, number, worktree.path, run.path, root)
        code = run_setup(run, settings.setup, worktree.path, env)
        if code != 0:
            _status(herdr, run, worktree.workspace_id, status.text(status.STOPPED, "setup failed"))
            alerts.notify(run, herdr, "setup failed", f"see {run.path / 'setup.log'}")
            run.event("run.end", outcome="failed", reason="setup")
            raise StartError(
                f"setup failed with exit {code}; see {run.path / 'setup.log'}. Fix it, then run"
                f" `placido start {arg}` again to retry setup in the same worktree"
            )
    _status(herdr, run, worktree.workspace_id, status.text(status.READY, "run placido spec"))
    run.event("run.ready", worktree=str(worktree.path))
    if not launch:
        return
    # Herdr opens the workspace without moving the user there; once setup is done,
    # switch to it, where the pane is already in the worktree (the user's ask).
    show_workspace(run, herdr, {"workspace": worktree.workspace_id})
    _launch_spec(herdr, run, worktree.pane_id)


def stays(created: dict, cwd: Path) -> bool:
    """Whether a command typed in cwd goes on where it was typed: in the run's worktree,
    or in its workspace's own pane, rather than in the run's workspace."""

    if created.get("pane") and os.environ.get("HERDR_PANE_ID") == created["pane"]:
        return True
    if not created.get("path"):
        return False
    try:
        top = Path(git(cwd, "rev-parse", "--show-toplevel"))
    except StartError:
        return False
    return top.resolve() == Path(created["path"]).resolve()


def hand_over(run: runlog.Run, herdr: Herdr, created: dict, command: str) -> str | None:
    """Go on with the run in its own workspace, switching the user there: "going" when a
    placido process already works on it, "handed" once the command is typed into the
    workspace's pane, whose shell is in the worktree. The terminal it was typed in is
    free at once, and Herdr's sidebar shows each run once, under its repository (the
    user's ask, 2026-10-17). None when Herdr cannot, so the command goes on where it
    was typed."""

    if driver.holder(run.path) is not None:
        show_workspace(run, herdr, created)
        return "going"
    pane = str(created.get("pane", ""))
    try:
        try:
            herdr.run_in_pane(pane, command)
        except HerdrError:  # the pane was closed: a new tab in the workspace
            pane = herdr.create_tab(str(created.get("workspace", "")), Path(created["path"]), run.path.parent.name)
            herdr.run_in_pane(pane, command)
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))
        return None
    run.event("run.handed_off", pane=pane, command=command)
    show_workspace(run, herdr, created)
    return "handed"


def show_workspace(run: runlog.Run, herdr: Herdr, created: dict) -> None:
    try:
        herdr.focus_workspace(str(created.get("workspace", "")))
        run.event("workspace.focused", workspace=created.get("workspace"))
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))


SPEC = "placido spec"


def _launch_spec(herdr: Herdr, run: runlog.Run, pane: str) -> None:
    """Go straight on with the interview in the issue's pane, whose shell is in the
    worktree (the user's ask, 2026-10-07). If Herdr cannot, next_steps says what to type."""

    try:
        herdr.run_in_pane(pane, SPEC)
        run.event("spec.launched", pane=pane)
    except HerdrError as error:
        run.event("herdr.warning", error=str(error))


def ensure_workspace(run_dir: Path, herdr: Herdr) -> dict:
    """The run's worktree.created record, after reopening the worktree as a workspace if
    its workspace was closed. Closing an issue's workspace from Herdr's sidebar leaves
    the worktree, branch, and stack in place, but every step opens its tab in that
    workspace (#404, 2026-10-02)."""

    created = runlog.last_event(run_dir, "worktree.created") or {}
    workspace, path = str(created.get("workspace", "")), Path(created.get("path", ""))
    if not workspace or not path.is_dir() or herdr.workspace_exists(workspace):
        return created
    record = runlog.read_record(run_dir)
    reopened = herdr.open_worktree(Path(record.get("repo") or path), path, run_dir.parent.name)
    run = runlog.Run(run_dir)
    run.event(
        "worktree.created", path=str(reopened.path), branch=reopened.branch or created.get("branch"),
        workspace=reopened.workspace_id, pane=reopened.pane_id, reopened=True, was=workspace,
    )
    return runlog.last_event(run_dir, "worktree.created") or created


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
    following = "placido run" if spec.sealed(spec.issue_dir(run.path)) else SPEC
    focused = runlog.last_event(run.path, "workspace.focused")
    if runlog.last_event(run.path, "run.handed_off"):
        if not runlog.last_event(run.path, "run.ready"):
            place = "Herdr has switched to the issue's workspace" if focused else "In the issue's workspace in Herdr"
            return f"{place}, setup goes on in the worktree ({created.get('path', '?')}), then {SPEC}.\n"
        if not runlog.last_event(run.path, "spec.launched"):
            return f"Next, here: {following}\n"
    if following == SPEC and runlog.last_event(run.path, "spec.launched"):
        place = "Herdr has switched to the issue's workspace" if focused else "In the issue's workspace in Herdr"
        return f"{place}, {SPEC} is starting in the worktree ({created.get('path', '?')}).\n"
    where = ("Herdr has switched to the issue's workspace, whose pane is in the worktree."
             if focused else "The issue's workspace in Herdr has a pane in the worktree.")
    return f"{where} Next, there or here:\n  cd {created.get('path', '?')}\n  {following}\n"


def failed_setup(root: Path, runs_root: Path, issue: str, branch: str, base: str) -> Path | None:
    """The issue's latest run when its setup failed and nothing has happened since:
    not closed, its worktree still there, and no commits on the branch beyond the
    base. Starting the issue again retries setup in that worktree."""

    mine = _issue_runs(root, runs_root, issue)
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


def handed_off(root: Path, runs_root: Path, issue: str) -> Path | None:
    """The issue's latest run when a start handed it to its workspace and its setup has
    not finished there: never ready, never ended, its worktree still there."""

    mine = _issue_runs(root, runs_root, issue)
    if not mine:
        return None
    events = {e.get("event") for e in runlog.read_events(mine[0])}
    created = runlog.last_event(mine[0], "worktree.created") or {}
    if (
        "run.handed_off" not in events or events & {"run.ready", "run.end", "run.closed"}
        or not created.get("path") or not Path(created["path"]).is_dir()
    ):
        return None
    return mine[0]


def _issue_runs(root: Path, runs_root: Path, issue: str) -> list[Path]:
    return [
        run for run in runlog.all_runs(runs_root)
        if run.parent.parent.name == root.name and run.parent.name == issue
        and runlog.read_record(run).get("repo") in (None, str(root))
    ]


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


def run_env(
    project: str, issue: str, number: int | None, worktree: Path, run_dir: Path, repo: Path,
) -> dict[str, str]:
    """What project commands and agents are told about the run, as PLACIDO_* variables.
    PLACIDO_REPO is the main checkout, so a command can run the project's scripts as they
    are there, not as the branch has them: a pull request branched before a fix to its
    setup script would otherwise run it without the fix (#598, 2026-10-17)."""

    name = re.sub(r"[^a-z0-9_-]+", "-", f"{project}-{number or issue}".lower()).strip("-")
    return {
        "PLACIDO_ISSUE": issue,
        "PLACIDO_ISSUE_NUMBER": str(number) if number else "",
        "PLACIDO_NAME": name,
        "PLACIDO_WORKTREE": str(worktree),
        "PLACIDO_RUN_DIR": str(run_dir),
        "PLACIDO_REPO": str(repo),
    }


def env_of(run_dir: Path) -> dict[str, str]:
    """The PLACIDO_* variables of an existing run, from its run.json and worktree."""

    record = runlog.read_record(run_dir)
    created = runlog.last_event(run_dir, "worktree.created") or {}
    return run_env(
        record.get("project", ""), record.get("issue", ""), record.get("issue_number"),
        Path(created.get("path", "")), run_dir, Path(record.get("repo", "")),
    )


def run_setup(run: runlog.Run, command: str, cwd: Path, env: dict[str, str]) -> int:
    """Run the project's setup command in the worktree, its output in setup.log."""

    return run_project_command(run, "setup", command, cwd, env)


def run_project_command(run: runlog.Run, name: str, command: str, cwd: Path, env: dict[str, str]) -> int:
    """Run one of the project's own commands, such as setup or teardown, in the
    worktree with the run's variables; its output goes to <name>.log."""

    run.event(f"{name}.start", command=command)
    began = time.monotonic()
    code = proc.run_shell(command, cwd, env, run.path / f"{name}.log", lambda s, said: run.progress(f"{name} running", s, said))
    run.event(f"{name}.end", exit=code, seconds=round(time.monotonic() - began, 1))
    return code


def _status(herdr: Herdr, run: runlog.Run, workspace_id: str, text: str) -> None:
    status.show(run.path, text)


