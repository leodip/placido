"""The placido command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from placido import __version__, checks, config, doctor, driver, followups, github, implement, mutate, report, review, runlog, spec, start, status, step, summary
from placido import close as closing
from placido import retro as retrospect
from placido.herdr import Herdr, HerdrError


def cmd_email_test(args: argparse.Namespace) -> int:
    from placido import alerts

    try:
        outcome = alerts.test()
    except config.ConfigError as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    print(outcome)
    return 0 if outcome.startswith("sent") else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor.run_checks()
    print(doctor.format_checks(checks, color=sys.stdout.isatty()))
    return 1 if doctor.failed(checks) else 0


def cmd_config(args: argparse.Namespace) -> int:
    start = Path(args.project or ".").resolve()
    try:
        resolved = config.load(config.find_config(start))
    except config.ConfigError as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    print(config.describe(resolved, start))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    try:
        run = start.start(args.issue, Path.cwd(), echo=sys.stdout)
    except (start.StartError, config.ConfigError) as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    print(f"\nrun folder: {run.path}\n{start.next_steps(run)}", end="")
    return 0


def _active(args: argparse.Namespace) -> tuple[Path, config.Config, Path, dict] | None:
    """The project's settings and the run a step belongs to, or None after an error."""

    try:
        root = start.project_root(Path.cwd())
        settings = config.load(config.find_config(root))
    except (start.StartError, config.ConfigError) as error:
        print(f"placido: {error}", file=sys.stderr)
        return None
    runs_root = runlog.runs_dir()
    try:
        path = runlog.resolve_run(args.run, runs_root) if args.run else start.select_run(Path.cwd(), runs_root)
    except start.StartError as error:
        print(f"placido: {error}", file=sys.stderr)
        return None
    created = runlog.last_event(path, "worktree.created")
    if created is None:
        print(f"placido: {path} is not a run with a worktree", file=sys.stderr)
        return None
    return root, settings, path, created


def _runner(path: Path, created: dict, settings: config.Config) -> tuple[runlog.Run, step.Step]:
    """The run and a Step that opens its tabs in the issue's workspace, reopened first
    if it was closed from Herdr's sidebar. Built only once a step will run, after a
    command's own refusals, so they never touch Herdr."""

    herdr = Herdr()
    try:
        created = start.ensure_workspace(path, herdr)
    except HerdrError as error:
        print(f"placido: the issue's workspace is gone and could not be reopened: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    run = runlog.Run(path, echo=sys.stdout)
    runner = step.Step(
        run, herdr, created["workspace"], Path(created["path"]), env=start.env_of(path),
        quota_wait=settings.quota_wait,
    )
    return run, runner


def _agent_for(args: argparse.Namespace, settings: config.Config, role: str) -> config.AgentSpec | None:
    """The role's agent, with any one-off --agent, --model, or --effort applied."""

    try:
        return config.override(settings.roles[role].agent, args.agent, args.model, args.effort)
    except config.ConfigError as error:
        print(f"placido: {error}", file=sys.stderr)
        return None


def _log_override(run: runlog.Run, args: argparse.Namespace, agent: config.AgentSpec) -> None:
    given = {k: v for k, v in (("agent", args.agent), ("model", args.model), ("effort", args.effort)) if v}
    if given:
        run.event("agent.override", **given, resolved=agent.command())


def cmd_step(args: argparse.Namespace) -> int:
    """Run one role's agent on a task in the project's active run."""

    found = _active(args)
    if found is None:
        return 1
    _, settings, path, created = found
    agent = _agent_for(args, settings, args.role)
    if agent is None:
        return 1
    run, runner = _runner(path, created, settings)
    _log_override(run, args, agent)
    try:
        result = runner(args.role, agent, " ".join(args.task))
    except HerdrError as error:
        run.event("step.failed", error=str(error))
        print(f"placido: {error}", file=sys.stderr)
        return 1
    print(f"\nstep folder: {result.path}")
    return 0 if result.outcome == "success" else 1


def cmd_spec(args: argparse.Namespace) -> int:
    """Interview the user about the run's issue, then check and seal the agreement."""

    found = _active(args)
    if found is None:
        return 1
    _, settings, path, created = found
    folder = spec.issue_dir(path)
    if spec.sealed(folder):
        print(f"placido: the agreement in {folder} is already sealed; remove seal.json to redo it", file=sys.stderr)
        return 1
    agent = _agent_for(args, settings, "spec")
    if agent is None:
        return 1
    run, runner = _runner(path, created, settings)
    _log_override(run, args, agent)
    task = spec.prompt(path, Path(created["path"]), settings)
    try:
        result = runner(
            "spec", agent, task, interactive=True,
            check=lambda result: spec.check(folder, settings),
        )
    except HerdrError as error:
        run.event("step.failed", error=str(error))
        print(f"placido: {error}", file=sys.stderr)
        return 1
    if result.outcome != "success":
        runner.status(status.text(status.STOPPED, f"spec {result.outcome.replace('-', ' ')}"))
        print(f"\nplacido: the specification ended {result.outcome}; see {result.path}", file=sys.stderr)
        return 1
    spec.seal(folder, run)
    runner.status(status.text(status.READY, "run placido run"))
    slices = spec.load_slices(folder)
    count = f"{len(slices)} slice{'s' if len(slices) != 1 else ''}"
    print(f"\nagreement sealed: {folder / 'agreement.md'} ({count})")
    return 0


def _slice_context(
    args: argparse.Namespace, role: str = "implement"
) -> tuple[driver.Context, list[dict]] | None:
    """What building slices needs, or None after reporting why it cannot start."""

    found = _active(args)
    if found is None:
        return None
    _, settings, path, created = found
    folder = spec.issue_dir(path)
    if not spec.sealed(folder):
        print(f"placido: no sealed agreement in {folder}; run `placido spec` first", file=sys.stderr)
        return None
    agent = _agent_for(args, settings, role)
    if agent is None:
        return None
    run, runner = _runner(path, created, settings)
    _log_override(run, args, agent)
    ctx = driver.Context(
        run, runner, runner.herdr, settings, agent, Path(created["path"]), start.env_of(path)
    )
    return ctx, spec.load_slices(folder)


def cmd_implement(args: argparse.Namespace) -> int:
    """Build one slice of the sealed agreement, check it, and commit it."""

    found = _slice_context(args)
    if found is None:
        return 1
    ctx, slices = found
    if not (args.agent or args.model or args.effort):
        ctx.agent = driver.implement_chain(ctx).current  # the agent the run had moved to, if any
    try:
        entry = implement.choose(slices, implement.committed(ctx.run.path), args.slice)
        with driver.Lock(ctx.run.path):
            driver.recover(ctx, slices)
            if entry["id"] in implement.committed(ctx.run.path):
                return _report_slice(ctx, entry, "committed")  # salvaged by the recovery
            try:
                outcome = driver.build_slice(ctx, entry)
            except KeyboardInterrupt:
                driver.stopping(ctx, slices)
                raise
            if outcome != "committed":
                driver.after_failure(ctx, entry["id"], outcome)
    except (implement.ImplementError, driver.RunError) as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    except HerdrError as error:
        ctx.run.event("step.failed", error=str(error))
        print(f"placido: {error}", file=sys.stderr)
        return 1
    return _report_slice(ctx, entry, outcome)


def _report_slice(ctx: driver.Context, entry: dict, outcome: str) -> int:
    if outcome == "committed":
        event = runlog.last_event(ctx.run.path, "slice.committed") or {}
        print(f"\nslice {entry['id']} committed: {str(event.get('commit', ''))[:7]} {entry['title']}")
        return 0
    print(f"\nplacido: slice {entry['id']} ended {outcome}", file=sys.stderr)
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    """Build every remaining slice in blocker order, resuming an interrupted run, then
    review the whole change."""

    found = _slice_context(args)
    if found is None:
        return 1
    ctx, slices = found
    try:
        with driver.Lock(ctx.run.path):
            try:
                outcome = driver.drive(ctx, slices)
            except KeyboardInterrupt:
                driver.stopping(ctx, slices)
                raise
            done = implement.committed(ctx.run.path)
            print(f"\n{len(done)} of {len(slices)} slices committed; the run is {outcome}.")
            if outcome != "done":
                driver.show_stopped(ctx, outcome)
                summary.write(ctx.run.path, stopped=outcome)
                return 1
            if checks.final_gates(ctx, ctx.settings.roles["fix"].agent) == "failed":
                driver.show_stopped(ctx, "final-gates")
                driver.notify(ctx, "the final gates fail", f"see {ctx.run.path / 'final-gates'}")
                summary.write(ctx.run.path, stopped="the final gates fail")
                print("\nplacido: the final gates still fail after the fixes; see the run log.", file=sys.stderr)
                return 1
            if not ctx.settings.final_rounds:
                if not runlog.last_event(ctx.run.path, "run.end"):
                    ctx.run.event("run.end", outcome="done", reason="no review configured")
                driver.show(ctx, status.DONE, "slices built")
                _summarize(ctx)
                return 0
            done = runlog.last_event(ctx.run.path, "review.done")
            if done:
                if runlog.last_event(ctx.run.path, "run.end"):
                    _summarize(ctx)
                    return 0
                if done.get("outcome") in ("passed", "escalated"):  # stopped while delivering
                    return _deliver(ctx, done["outcome"])
            return _review(ctx, ctx.settings.roles["review"].agent)
    except driver.RunError as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    except HerdrError as error:
        ctx.run.event("step.failed", error=str(error))
        print(f"placido: {error}", file=sys.stderr)
        return 1


def cmd_review(args: argparse.Namespace) -> int:
    """Review the change, fix what the review finds, and verify the fixes in rounds."""

    found = _slice_context(args, role="review")
    if found is None:
        return 1
    ctx, _ = found
    try:
        with driver.Lock(ctx.run.path):
            return _review(ctx, ctx.agent)
    except driver.RunError as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    except HerdrError as error:
        ctx.run.event("step.failed", error=str(error))
        print(f"placido: {error}", file=sys.stderr)
        return 1


def _review(ctx: driver.Context, reviewer: config.AgentSpec) -> int:
    loop = review.Loop(
        ctx.run, ctx.runner, ctx.herdr, ctx.settings, reviewer, ctx.settings.roles["fix"].agent,
        ctx.worktree, ctx.env,
    )
    account = loop()
    ctx.run.event(
        "review.done", outcome=account.outcome, rounds=len(account.rounds),
        escalated=len(account.escalated), followups=len(account.followups),
    )
    ctx.run.event("review.followups", items=[
        {key: item.get(key) for key in ("id", "title", "description", "why")} for item in account.followups
    ])
    print("\n" + review.summary(account, ctx.run.path))
    if account.outcome not in ("passed", "escalated"):
        driver.show(ctx, status.STOPPED, "review failed")
        driver.notify(ctx, "the review failed", f"run placido run again to resume; see {ctx.run.path / 'review.md'}")
        _summarize(ctx)
        return 1
    if account.escalated:
        driver.notify(ctx, "review needs your decision",
                      f"{len(account.escalated)} finding(s); see {ctx.run.path / 'review.md'}")
    return _deliver(ctx, account.outcome)


def _deliver(ctx: driver.Context, outcome: str) -> int:
    """Hand the reviewed change over: push it and open the pull request, wait for CI,
    then end the run. Its work is done once CI is green, or there is no CI or GitHub;
    what is left (trying the branch, merging, escalations) is the user's."""

    followups.draft(ctx, ctx.settings.roles["fix"].agent)
    try:
        ci = github.deliver(ctx, ctx.settings.roles["fix"].agent)
    except github.GitHubError as error:
        ctx.run.event("github.failed", error=str(error))
        driver.show(ctx, status.STOPPED, "GitHub failed")
        driver.notify(ctx, "delivery to GitHub failed", f"{str(error)[:160]}; run placido run again to retry")
        _summarize(ctx)
        print(f"placido: {error}\nFix it, then run placido run again to retry.", file=sys.stderr)
        return 1
    pr = (runlog.last_event(ctx.run.path, "pr.opened") or {}).get("url", "")
    if ci in ("red", "pending"):
        driver.show(ctx, status.STOPPED if ci == "red" else status.WAITING, f"CI {ci} on the pull request")
        driver.notify(ctx, f"CI is {ci}", f"{pr}; run placido run again to retry")
        _summarize(ctx)
        print(f"\nplacido: CI is {ci} on {pr}; run placido run again once it is sorted.", file=sys.stderr)
        return 1
    ctx.run.event("run.end", outcome=outcome, ci=ci, pr=pr or None)
    if outcome == "escalated":
        driver.show(ctx, status.YOU, "review needs your decisions")
    else:
        driver.show(ctx, status.DONE, f"pull request {pr.rsplit('/', 1)[-1]}" if pr else "review passed")
    if pr:
        driver.notify(ctx, "the pull request is ready", f"{pr} · CI {ci}")
        print(f"\npull request: {pr} (CI {ci})")
    _summarize(ctx)
    return 0 if outcome == "passed" else 1


def cmd_retro(args: argparse.Namespace) -> int:
    """Look back on a run and suggest project improvements, changing nothing."""

    try:
        root = start.project_root(Path.cwd())
        settings = config.load(config.find_config(root))
        runs_root = runlog.runs_dir()
        path = runlog.resolve_run(args.run, runs_root) if args.run else start.select_run(Path.cwd(), runs_root)
    except (start.StartError, config.ConfigError) as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    agent = _agent_for(args, settings, "retro")
    if agent is None:
        return 1
    created = runlog.last_event(path, "worktree.created") or {}
    worktree = Path(created.get("path", ""))
    if created.get("path") and worktree.is_dir():
        workspace, code = created["workspace"], worktree
    else:  # a closed run: the agent works from this pane's workspace and the main checkout
        pane = os.environ.get("HERDR_PANE_ID", "")
        if not pane:
            print("placido: the run is closed; run placido retro from a Herdr pane", file=sys.stderr)
            return 1
        workspace, code = pane.split(":")[0], root
    run = runlog.Run(path, echo=sys.stdout)
    runner = step.Step(run, Herdr(), workspace, code, env=start.env_of(path), quota_wait=settings.quota_wait)
    _log_override(run, args, agent)
    try:
        result = retrospect.retro(runner, agent, path, code)
    except HerdrError as error:
        run.event("step.failed", error=str(error))
        print(f"placido: {error}", file=sys.stderr)
        return 1
    if result.outcome != "success":
        print(f"\nplacido: the retro ended {result.outcome}; see {result.path}", file=sys.stderr)
        return 1
    print("\n" + (path / "retro.md").read_text(encoding="utf-8"))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    """Clean up an issue: teardown, its worktree and workspace, then the base pulled and
    the branch deleted once its work is merged."""

    try:
        root = start.project_root(Path.cwd())
        settings = config.load(config.find_config(root))
        runs_root = runlog.runs_dir()
        if args.run:
            path = runlog.resolve_run(args.run, runs_root)
        elif args.issue:
            path = start.find_run(root, runs_root, args.issue, unready=True)
        else:
            path = start.select_run(Path.cwd(), runs_root, unready=True)
    except (start.StartError, config.ConfigError) as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    created = runlog.last_event(path, "worktree.created") or {}
    worktree = Path(created.get("path", "")).resolve()
    inside = Path.cwd().resolve().is_relative_to(worktree) if created.get("path") else False
    run = runlog.Run(path, echo=sys.stdout)
    try:
        closing.close(run, Herdr(), settings, args.force, os.environ.get("HERDR_PANE_ID"))
    except closing.CloseError as error:
        print(f"placido: {error}", file=sys.stderr)
        return 1
    branch = runlog.read_record(path).get("branch", "")
    print(f"\nclosed {path.parent.name}: its worktree and workspace are gone.")
    pulled = runlog.last_event(path, "base.pulled")
    skipped = runlog.last_event(path, "base.pull_skipped")
    if pulled:
        print(f"{pulled.get('base')} is up to date ({pulled.get('before')[:7]} → {pulled.get('after')[:7]}).")
    elif skipped:
        print(f"{runlog.read_record(path).get('base') or 'The base'} was not pulled: {skipped.get('reason')}.")
    deleted = runlog.last_event(path, "branch.deleted")
    kept = runlog.last_event(path, "branch.kept")
    if deleted:
        print(f"The branch {branch} is deleted: {deleted.get('reason')}.")
    elif kept:
        print(f"The branch {branch} stays: {kept.get('reason')}.")
    if inside:
        print(f"This shell's folder is gone with the worktree: cd {root}")
    return 0


def _summarize(ctx: driver.Context) -> None:
    summary.write(ctx.run.path)
    print(f"summary: {ctx.run.path / 'summary.md'}")


def cmd_mutate(args: argparse.Namespace) -> int:
    return mutate.main(args.file, args.replace, args.with_, args.test_command, args.timeout)


def cmd_trial(args: argparse.Namespace) -> int:
    """Scaffolding until `placido start`: make a run folder and log a pretend step."""

    run = runlog.Run.create(
        runlog.runs_dir(),
        "trial",
        "00-trial",
        {"placido": __version__, "versions": doctor.tool_versions()},
        echo=sys.stdout,
    )
    run.event("run.start", project="trial", issue="00-trial")
    step = run.step_dir(1, "trial-note")
    run.event("step.start", step=step.name)
    (step / "prompt.md").write_text("Write a short note.\n", encoding="utf-8")
    run.event("step.end", step=step.name, outcome="success")
    run.event("run.end", outcome="success")
    print(f"\nrun folder: {run.path}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    root = runlog.runs_dir()
    runs = runlog.all_runs(root)[: args.limit]
    if not runs:
        print(f"no runs under {root}")
        return 0
    names = [run.relative_to(root).as_posix() for run in runs]
    width = max(len(name) for name in names)
    for run, name in zip(runs, names):
        print(f"{name.ljust(width)}  {runlog.outcome(run)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Every issue in progress, across projects, with its status."""

    print(status.board(runlog.runs_dir()), end="")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """What a run spent and how it went, or trends across runs."""

    root = runlog.runs_dir()
    try:
        project = start.project_root(Path.cwd())
    except start.StartError:
        project = None  # outside a repository: every project's runs
    if args.runs:
        runs = runlog.all_runs(root)
        if project is not None and not args.all:
            runs = [r for r in runs if _run_of(r, project)]
        reports = sorted((report.build(r) for r in runs), key=lambda r: r.started or datetime.min.astimezone())
        reports = reports[-args.limit:]
        if args.json:
            print(json.dumps([report.as_dict(r) for r in reports], indent=2))
        else:
            print(report.trends(reports), end="")
        return 0
    if args.run:
        path = runlog.resolve_run(args.run, root)
    elif project is not None:
        active = runlog.active_runs(root, project.name, str(project))
        mine = [r for r in runlog.all_runs(root) if _run_of(r, project)]
        if len(active) > 1:
            try:
                path = start.select_run(Path.cwd(), root)
            except start.StartError as error:
                print(f"placido: {error}", file=sys.stderr)
                return 1
        else:
            path = active[0] if active else (mine[0] if mine else None)
    else:
        path = runlog.latest_run(root)
    if path is None or not (path / "run.json").exists():
        print(f"placido: no run found{f' at {path}' if path else ''}", file=sys.stderr)
        return 1
    built = report.build(path)
    print(json.dumps(report.as_dict(built), indent=2) if args.json else report.render(built), end="")
    return 0


def _run_of(run: Path, project: Path) -> bool:
    """Whether a run belongs to this repository, as its run.json records."""

    try:
        return runlog.read_record(run).get("repo") == str(project)
    except (OSError, ValueError):
        return False


def cmd_log(args: argparse.Namespace) -> int:
    root = runlog.runs_dir()
    path = runlog.resolve_run(args.run, root) if args.run else runlog.latest_run(root)
    if path is None or not (path / "run.json").exists():
        print(f"placido: no run found{f' at {path}' if path else ''}", file=sys.stderr)
        return 1
    print(f"run folder: {path}\n")
    for event in runlog.read_events(path):
        print(runlog.format_event(event))
    return 0


def _override_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("one-off changes to the role's agent, for this command only")
    group.add_argument("--agent", help="claude, codex, or pi")
    group.add_argument("--model", help="the model, as the agent names it")
    group.add_argument("--effort", help="low, medium, high, xhigh, or max")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="placido", description="An AI coding workflow on Herdr.")
    parser.add_argument("--version", action="version", version=f"placido {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.add_parser(
        "doctor", help="check herdr, the agents, their logins and billing"
    ).set_defaults(func=cmd_doctor)
    begin = commands.add_parser(
        "start", help="start work on an issue: a worktree and Herdr workspace, then setup"
    )
    begin.add_argument("issue", help="an issue file in issues/, by name or unique prefix")
    begin.set_defaults(func=cmd_start)
    interview = commands.add_parser(
        "spec", help="interview the user about the issue, then seal the agreement and slices"
    )
    interview.add_argument("--run", help="a run as `placido runs` lists it (default: the active run)")
    _override_options(interview)
    interview.set_defaults(func=cmd_spec)
    build = commands.add_parser("implement", help="build one slice of the sealed agreement, check it, commit it")
    build.add_argument("slice", nargs="?", type=int, help="the slice (default: the next ready one)")
    build.add_argument("--run", help="a run as `placido runs` lists it (default: the active run)")
    _override_options(build)
    build.set_defaults(func=cmd_implement)
    check = commands.add_parser("review", help="review the change, fix the findings, verify in rounds")
    check.add_argument("--run", help="a run as `placido runs` lists it (default: the active run)")
    _override_options(check)
    check.set_defaults(func=cmd_review)
    look = commands.add_parser("retro", help="look back on a run and suggest project improvements")
    look.add_argument("--run", help="a run as `placido runs` lists it (default: the active run)")
    _override_options(look)
    look.set_defaults(func=cmd_retro)
    shut = commands.add_parser(
        "close", help="clean up an issue: teardown, worktree, workspace (the branch stays)"
    )
    shut.add_argument("issue", nargs="?", help="the issue, in full or by a unique prefix (default: this worktree's)")
    shut.add_argument("--run", help="a run as `placido runs` lists it")
    shut.add_argument("--force", action="store_true",
                      help="discard uncommitted changes, and go on past a failing teardown")
    shut.set_defaults(func=cmd_close)
    drive = commands.add_parser("run", help="build every remaining slice in blocker order, then review; resumes")
    drive.add_argument("--run", help="a run as `placido runs` lists it (default: the active run)")
    _override_options(drive)
    drive.set_defaults(func=cmd_run)
    one = commands.add_parser("step", help="run one role's agent on a task in the active run")
    one.add_argument("role", choices=config.ROLES)
    one.add_argument("task", nargs="+", help="what the agent should do")
    one.add_argument("--run", help="a run as `placido runs` lists it (default: the active run)")
    _override_options(one)
    one.set_defaults(func=cmd_step)
    breaker = commands.add_parser(
        "mutate", help="break code on purpose and check the tests notice, then restore it"
    )
    breaker.add_argument("file")
    breaker.add_argument("--replace", required=True, help="exact text to replace; must match once")
    breaker.add_argument("--with", dest="with_", required=True, help="the broken text")
    breaker.add_argument("--timeout", type=float, default=900, help="seconds (default 900)")
    breaker.set_defaults(func=cmd_mutate, test_command=[])
    breaker.epilog = "Give the test command after --, for example: -- python3 -m unittest -q"
    show = commands.add_parser("config", help="show the resolved configuration and agent commands")
    show.add_argument("--project", help="a folder in the project (default: the current folder)")
    show.set_defaults(func=cmd_config)
    commands.add_parser(
        "trial", help="make a trial run folder and event log"
    ).set_defaults(func=cmd_trial)
    runs = commands.add_parser("runs", help="list runs, newest first, with their outcomes")
    runs.add_argument("-n", "--limit", type=int, default=20, help="how many to list (default 20)")
    runs.set_defaults(func=cmd_runs)
    tally = commands.add_parser("report", help="what a run spent and how it went, or trends across runs")
    tally.add_argument("run", nargs="?", help="a run as `placido runs` lists it (default: this project's current or latest)")
    tally.add_argument("--runs", action="store_true", help="trends across this project's runs")
    tally.add_argument("--all", action="store_true", help="with --runs, every project's runs")
    tally.add_argument("-n", "--limit", type=int, default=20, help="with --runs, the latest N (default 20)")
    tally.add_argument("--json", action="store_true", help="the same data as JSON")
    tally.set_defaults(func=cmd_report)
    mail = commands.add_parser("email-test", help="send a test email with the current quota")
    mail.set_defaults(func=cmd_email_test)
    glance = commands.add_parser("status", help="every issue in progress, across projects, with its status")
    glance.set_defaults(func=cmd_status)
    log = commands.add_parser("log", help="print a run's events, the latest run by default")
    log.add_argument("run", nargs="?", help="a run folder, or a run as `placido runs` lists it")
    log.set_defaults(func=cmd_log)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    # Everything after `--` is the test command of `placido mutate`, never placido's options.
    test_command: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, test_command = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if test_command:
        args.test_command = test_command
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nplacido: interrupted. Run the same command again to resume.", file=sys.stderr)
        return 130
