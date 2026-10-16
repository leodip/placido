"""The end of `placido pr`: the review's summary, and the actions the user picks.

Nothing reaches GitHub or the contributor's branch until the user picks it here, and
pushing, merging, and closing ask once more (decided 2026-10-16). A merge is pinned to
the commit the review covered, or to the fixes made on it here, so nothing pushed in
between is merged unreviewed. Fixes made here go on the contributor's own branch, so
the pull request stays the one way the change lands and keeps its author.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from placido import config, decisions, fallback, github, implement, pr, pragents, prcheck, prgh, runlog, status
from placido.pr import Context, PRStop, Round

NO_CHANGES = "No code changes"
PUSH_URL = "https://github.com/{repo}.git"  # where the contributor's branch is pushed to
RESOLUTIONS = ("fixed", "disputed")
ORDER = ("edit", "edit-merge", "fix", "push", "post", "ci", "merge", "close")

Ask = Callable[[str], "bool | None"]


def final(rnd: Round) -> dict[str, Any]:
    return json.loads((rnd.folder / "final.json").read_text(encoding="utf-8"))


@dataclass
class State:
    """What the actions depend on, read afresh before each menu."""

    pull: prgh.PullRequest
    verdict: str
    route: str
    kept: list[dict[str, Any]]
    fixed: bool  # the fix ran this round, whether or not it changed code
    fixes: list[str]  # commits made here this round
    ahead: int  # commits here that the pull request's branch lacks
    pushed: bool
    posted: bool
    waiting: list[dict[str, Any]]  # CI runs held for approval
    ci: str  # green, red, pending, or none


def state(ctx: Context, rnd: Round) -> State:
    number = ctx.pr.number
    pull = ctx.hub.pull_request(number)
    ctx.pr = pull
    if pull.state == "OPEN":
        pr.fetch(ctx.root, pull.base, number)
    data = final(rnd)
    run_dir = ctx.run.path
    fixes = pr.fix_commits(run_dir, rnd.number)
    ahead = int(pr.git(ctx.worktree, "rev-list", "--count", f"{pr.pr_ref(number)}..HEAD") or 0)
    try:
        waiting = ctx.hub.waiting_runs(pull.head_sha) if pull.state == "OPEN" else []
        ci = prgh.ci_state(ctx.hub.checks(number)) if pull.state == "OPEN" else "none"
    except prgh.PRError:
        waiting, ci = [], "unknown"
    return State(
        pull=pull,
        verdict=data["verdict"],
        route=data["routing"]["route"],
        kept=[f for f in data["findings"] if f["decision"] == "keep"],
        fixed=bool(pr.round_events(run_dir, "pr.fixed", rnd.number)),
        fixes=fixes,
        ahead=ahead,
        pushed=bool(pr.round_events(run_dir, "pr.pushed", rnd.number)),
        posted=bool(pr.round_events(run_dir, "pr.posted", rnd.number)),
        waiting=waiting,
        ci=ci,
    )


# ---- the summary --------------------------------------------------------------------------


def summary(ctx: Context, rnd: Round) -> str:
    data, run_dir, pull = final(rnd), ctx.run.path, ctx.pr
    lines = [
        f"# Pull request #{pull.number}: {pull.title}",
        "",
        f"{pull.url} · @{pull.author} · +{pull.additions} −{pull.deletions} in {pull.files} files"
        f" · round {rnd.number}, head {rnd.head[:12]}",
        "",
        f"Verdict: {data['verdict']}. {data['summary']}",
    ]
    routing = data["routing"]
    if routing["route"] != "none":
        whom = "the contributor" if routing["route"] == "contributor" else "here, by you"
        lines.append(
            f"Fixes: {whom} (about {routing['share']:.0%} of the pull request; the line is"
            f" {ctx.settings.pr.contributor_share:.0%}). {routing.get('estimate', '')}".rstrip()
        )
    scanned = pr.done(run_dir, "pr.scan", rnd.number) or {}
    scan_text = "clear" if scanned.get("verdict") == "clear" else f"{scanned.get('concerns', '?')} concern(s), accepted by you"
    lines.append(f"Scan: {scan_text}")
    gates = pr.done(run_dir, "pr.gates", rnd.number) or {}
    if gates.get("results"):
        shown = [f"{name} {'pass' if code == 0 else 'FAIL'}" for name, code in gates["results"].items()]
        changed = [e["after"] for e in pr.round_events(run_dir, "pr.tree_restored", rnd.number)
                   if e.get("after", "").endswith("gate")]
        lines.append("Gates: " + " · ".join(shown) + (f" (changed the tree: {', '.join(changed)})" if changed else ""))
    reviewed = [
        f"{e['letter']} {e['agent']} {e['model']} {e['effort']} ({e['findings']} findings"
        + ("" if e.get("configured", True) else ", a fallback") + ")"
        for e in pr.round_events(run_dir, "pr.reviewed", rnd.number)
    ]
    lines.append("Reviewers: " + "; ".join(reviewed))
    kept = [f for f in data["findings"] if f["decision"] == "keep"]
    dropped = [f for f in data["findings"] if f["decision"] == "drop"]
    if kept:
        lines += ["", "Findings:"]
        for f in kept:
            where = f" `{f['location']}`" if f.get("location") else ""
            lines.append(
                f"  {f['id']}  {f['severity']:<11} {f['area']:<11}{where} {f['title']}"
                f" [{', '.join(f['sources'])}: {f['agreement']}]"
            )
    if dropped:
        lines += ["", "Dropped:"]
        lines += [f"  {f['id']}  {f['title']} [{', '.join(f['sources'])}]: {f['reason']}" for f in dropped]
    if data.get("earlier"):
        lines += ["", "The round before:"]
        lines += [f"  {e['item']}: {e['status']}" + (f" ({e['note']})" if e.get("note") else "") for e in data["earlier"]]
    if data.get("decisions"):
        lines += ["", "Decisions for you:"]
        lines += [f"  - {d.get('question', d)} → {d.get('recommendation', '')}" if isinstance(d, dict) else f"  - {d}"
                  for d in data["decisions"]]
    if data.get("preexisting"):
        lines += ["", "Found outside this change (for you only, never in the comment):"]
        for item in data["preexisting"]:
            flag = " [security]" if isinstance(item, dict) and item.get("security") else ""
            if isinstance(item, dict):
                where = f" ({item['location']})" if item.get("location") else ""
                lines.append(f"  - {item.get('title', '?')}{where}{flag}")
    fixed = pr.round_events(run_dir, "pr.fixed", rnd.number)
    if fixed:
        last = fixed[-1]
        lines += ["", f"Fixed here in {last['commit'][:12]}: {last.get('fixed', 0)} fixed, {last.get('disputed', 0)} disputed"
                  f" (see {run_dir / 'steps' / last['step'] / 'resolutions.json'})"]
    lines += [
        "",
        f"Full review: {rnd.folder / 'final.md'}",
        f"Comment draft: {rnd.folder / 'comment.md'}",
        "",
        "---- the comment to the contributor ----",
        (rnd.folder / "comment.md").read_text(encoding="utf-8").strip(),
        "---- end of the comment ----",
    ]
    return "\n".join(lines) + "\n"


# ---- the menu -------------------------------------------------------------------------------


def options(ctx: Context, rnd: Round, s: State) -> tuple[list[tuple[str, str]], list[str]]:
    """The actions open now, as (key, label), and notes on what is not."""

    if s.pull.state != "OPEN":
        note = f"The pull request is {s.pull.state.lower()}: placido close pr-{s.pull.number} ends the run."
        return [("quit", "Nothing more")], [note]
    out: list[tuple[str, str]] = []
    notes: list[str] = []
    out.append(("edit", f"Edit the comment ({rnd.folder / 'comment.md'})"))
    if (rnd.folder / "merge.md").is_file():
        out.append(("edit-merge", f"Edit the squash message ({rnd.folder / 'merge.md'})"))
    if s.kept and not s.fixed:
        out.append(("fix", f"Fix the {len(s.kept)} kept finding(s) here, on the contributor's branch:"
                           " committed locally, nothing pushed"))
    if s.ahead and s.pull.can_modify:
        out.append(("push", f"Push the {s.ahead} commit(s) made here to {s.pull.head_repo}:{s.pull.head_ref}"))
    elif s.ahead:
        notes.append("The contributor does not let maintainers push to the branch, so the fixes made"
                     " here cannot be pushed from placido.")
    if not s.posted:
        how = {"request-changes": "as a review requesting changes", "merge": "as an approving review",
               "fix-here": "as a comment", "close": "as a comment"}[s.verdict]
        out.append(("post", f"Post the comment {how}"))
    if s.waiting:
        out.append(("ci", f"Approve the {len(s.waiting)} CI run(s) waiting for a maintainer"))
    if not s.ahead and (s.verdict == "merge" or s.pushed or (s.fixed and not s.fixes)):
        out.append(("merge", f"Squash-merge into {s.pull.base}, pinned to {s.pull.head_sha[:12]} (CI {s.ci})"))
    out.append(("close", "Close the pull request, posting the comment if it is not posted"))
    out.append(("quit", f"Nothing more now: placido pr {s.pull.number} shows this again"))
    return out, notes


def menu(ctx: Context, rnd: Round, read: Callable[[str], str | None], confirm: Ask) -> None:
    """Show the actions open, run the ones picked, and again, until the user is done."""

    while True:
        s = state(ctx, rnd)
        listed, notes = options(ctx, rnd, s)
        print(f"\nCI {s.ci} · {'posted' if s.posted else 'not posted'} · "
              f"{s.ahead} commit(s) here not pushed · {len(s.waiting)} CI run(s) waiting")
        for note in notes:
            print(note)
        for n, (_, label) in enumerate(listed, 1):
            print(f"  {n}. {label}")
        answer = read("Pick one or more numbers (Enter: nothing): ")
        if answer is None or not answer.strip():
            return
        try:
            picked = {listed[int(part) - 1][0] for part in answer.replace(",", " ").split()}
        except (ValueError, IndexError):
            print("placido: pick numbers from the list")
            continue
        if "quit" in picked:
            picked.discard("quit")
            if not picked:
                return
        if {"merge", "close"} <= picked:
            print("placido: merge or close, not both")
            continue
        for key in [k for k in ORDER if k in picked]:
            try:
                ACTIONS[key](ctx, rnd, s, confirm)
            except (PRStop, prgh.PRError, implement.ImplementError) as error:
                print(f"placido: {key}: {error}")
                ctx.run.event("pr.action_failed", round=rnd.number, action=key, error=str(error)[:500])
                break
            s = state(ctx, rnd)


def _edit(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(editor), str(path)])


def edit(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    _edit(rnd.folder / "comment.md")
    ctx.run.event("pr.edited", round=rnd.number, file="comment.md")
    print((rnd.folder / "comment.md").read_text(encoding="utf-8"))


def edit_merge(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    _edit(rnd.folder / "merge.md")
    ctx.run.event("pr.edited", round=rnd.number, file="merge.md")


def _body(ctx: Context, rnd: Round) -> Path:
    """The comment as posted: the draft, then placido's hidden marker for later rounds."""

    text = (rnd.folder / "comment.md").read_text(encoding="utf-8").rstrip()
    path = rnd.folder / "posted-comment.md"
    path.write_text(f"{text}\n\n{prgh.marker(rnd.number, rnd.head)}\n", encoding="utf-8")
    return path


def post(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    if s.verdict == "fix-here" and not s.pushed:
        if not confirm("The comment says what was fixed on the branch, and nothing is pushed yet. Post it anyway?"):
            return
    kind = {"request-changes": "request-changes", "merge": "approve"}.get(s.verdict, "comment")
    ctx.hub.review(s.pull.number, kind, _body(ctx, rnd))
    ctx.run.event("pr.posted", round=rnd.number, kind=kind)
    print(f"posted on {s.pull.url}")


def push(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    if not s.pull.can_modify:
        raise PRStop("the contributor does not let maintainers push to the branch")
    target = f"{s.pull.head_repo}:{s.pull.head_ref}"
    if not confirm(f"Push {s.ahead} commit(s) to {target}? (never forced)"):
        return
    url = PUSH_URL.format(repo=s.pull.head_repo)
    done = subprocess.run(
        ["git", "-C", str(ctx.worktree), "push", url, f"HEAD:refs/heads/{s.pull.head_ref}"],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise PRStop(f"git push: {(done.stderr or done.stdout).strip()[-400:]}")
    commit = pr.git(ctx.worktree, "rev-parse", "HEAD")
    ctx.run.event("pr.pushed", round=rnd.number, commit=commit, to=target)
    print(f"pushed {commit[:12]} to {target}")


def approve_ci(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    workflows = pr.git(ctx.worktree, "diff", "--name-only", rnd.base, s.pull.head_sha, "--", ".github")
    if workflows and not confirm(f"The pull request changes {', '.join(workflows.splitlines())}, which these"
                                 " runs execute. Approve them anyway?"):
        return
    ids = [int(r["databaseId"]) for r in s.waiting]
    for run_id in ids:
        ctx.hub.approve_run(run_id)
    ctx.run.event("pr.ci_approved", round=rnd.number, runs=ids)
    print(f"approved {len(ids)} CI run(s)")


def merge(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    covered = {rnd.head, *(e["commit"] for e in pr.round_events(ctx.run.path, "pr.pushed", rnd.number))}
    head = s.pull.head_sha
    if head not in covered:
        raise PRStop(f"the pull request's head is {head[:12]}, which this review did not cover; run placido pr again")
    ci = s.ci
    if ci == "pending":
        if not confirm(f"CI is still running on {head[:12]}. Wait for it (up to {config.format_duration(ctx.settings.ci_wait)})?"):
            return
        ci, _ = github._await_ci(ctx.hub, s.pull.number, head, ctx.settings.ci_wait, time.sleep, time.monotonic,
                                 github._progress(ctx.run, f"CI on #{s.pull.number}"))
    if ci == "red":
        raise PRStop(f"CI is red on {head[:12]}; see {s.pull.url}/checks")
    if ci in ("none", "unknown") and not confirm(f"CI shows no result on {head[:12]}. Merge anyway?"):
        return
    drafted = rnd.folder / "merge.md"
    # Without the judge's draft (a request for changes fixed here instead), GitHub's own.
    text = drafted.read_text(encoding="utf-8") if drafted.is_file() else f"{s.pull.title} (#{s.pull.number})"
    lines = text.strip().splitlines()
    subject = lines[0].strip()
    body = rnd.folder / "merge-body.md"
    body.write_text("\n".join(lines[1:]).strip() + "\n", encoding="utf-8")
    if not confirm(f"Squash-merge #{s.pull.number} into {s.pull.base} as \"{subject}\"?"):
        return
    ctx.hub.merge(s.pull.number, head, subject, body)
    ctx.run.event("pr.merged", round=rnd.number, head=head, subject=subject)
    status.show(ctx.run.path, status.text(status.DONE, f"merged; placido close pr-{s.pull.number}"))
    print(f"merged #{s.pull.number}; placido close pr-{s.pull.number} tears its worktree and stack down")


def close_pr(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    if not confirm(f"Close #{s.pull.number}" + ("" if s.posted else ", posting the comment") + "?"):
        return
    if not s.posted:
        ctx.hub.comment(s.pull.number, _body(ctx, rnd))
        ctx.run.event("pr.posted", round=rnd.number, kind="comment")
    ctx.hub.close(s.pull.number)
    ctx.run.event("pr.closed_pr", round=rnd.number)
    status.show(ctx.run.path, status.text(status.DONE, f"closed; placido close pr-{s.pull.number}"))
    print(f"closed #{s.pull.number}")


# ---- fixing it here ---------------------------------------------------------------------------


def fix(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    """The fix role fixes every kept finding on the contributor's branch, here; placido
    runs the gates and commits. Nothing is pushed."""

    run, wt = ctx.run, ctx.worktree
    ids = [f["id"] for f in s.kept]
    (rnd.folder / "fix-findings.json").write_text(json.dumps({"findings": s.kept}, indent=2) + "\n", encoding="utf-8")
    head = pr.git(wt, "rev-parse", "HEAD")
    pr.verify_host_paths(ctx, rnd, "the fix")
    run.event("pr.fix_start", round=rnd.number, findings=ids)
    chain = fallback.Chain(
        run, "fix", fallback.entries(ctx.settings, "fix"), lambda title, body: pr.notify(ctx, title, body),
        since=lambda event: event.get("event") == "pr.fix_start",
    )
    entry = {"id": f"pr-{s.pull.number}", "title": "review fixes", "gates": list(ctx.settings.pr.gates)}
    settings = replace(ctx.settings, mutations=0)

    def check(result: Path) -> list[str]:
        if implement.blocked(result) is not None:
            return []
        problems = decisions.check(result.parent, run.path) or check_resolutions(result.parent / "resolutions.json", ids)
        if problems:
            return problems
        host = pr.changed_files(wt, *ctx.settings.pr.host_paths) if ctx.settings.pr.host_paths else []
        if host:
            return [f"you changed {', '.join(host)}, which run on this machine; undo those changes."]
        if result.read_text(encoding="utf-8").strip().splitlines()[:1] == [NO_CHANGES]:
            return [f"the result says '{NO_CHANGES}', but the worktree has changes."] if pr.git(wt, "status", "--porcelain") else []
        return implement.check(result, wt, head, entry, settings, ctx.env, run)

    name = f"r{rnd.number}-fix"
    step = pragents.attempt(ctx, chain, lambda spec: ctx.runner(
        "fix", spec, fix_prompt(ctx, rnd, pragents._next(run, name)), name=name, check=check, ask=implement.blocked,
    ))
    if step.outcome != "success":
        if pr.git(wt, "status", "--porcelain"):
            message = f"placido: pr fix failed ({step.outcome}, {run.path.name})"
            pr.git(wt, "stash", "push", "--include-untracked", "-m", message)
            run.event("worktree.stashed", message=message)
        raise PRStop(f"the fix did not finish ({step.outcome}); see {step.path}")
    data = json.loads((step.path / "resolutions.json").read_text(encoding="utf-8"))
    counts = {k: sum(1 for r in data["resolutions"] if r.get("status") == k) for k in RESOLUTIONS}
    result = step.path / "result.md"
    commit = None
    if result.read_text(encoding="utf-8").strip().splitlines()[:1] != [NO_CHANGES]:
        # The gates in the check ran the pull request's tests after the fixer's own check
        # of the host paths: whatever they wrote there must not ride into the commit.
        pr.verify_host_paths(ctx, rnd, "the fix's commit")
        commit = _commit(wt, result, run, rnd)
    run.event("pr.fixed", round=rnd.number, commit=commit, step=step.path.name, **counts)
    pr.settle(ctx, rnd, "the fix")
    print(f"\nfixes: {counts['fixed']} fixed, {counts['disputed']} disputed"
          + (f"; committed as {commit[:12]}, not pushed" if commit else "; no code changed"))
    print(f"their answers: {step.path / 'resolutions.json'}")
    if counts["disputed"]:
        print("The comment may name findings the fix disputed: edit it before posting.")


def check_resolutions(path: Path, ids: list[str]) -> list[str]:
    data, error = prcheck.load(path)
    if error:
        return [error]
    entries = data.get("resolutions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ['resolutions.json needs a "resolutions" list.']
    by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}
    problems = []
    for finding_id in ids:
        answer = by_id.get(finding_id)
        if answer is None:
            problems.append(f"finding {finding_id} has no resolution.")
        elif answer.get("status") not in RESOLUTIONS or not prcheck.texts(answer, "note"):
            problems.append(f"finding {finding_id} needs a status of {', '.join(RESOLUTIONS)} and a note.")
    return problems


def fix_prompt(ctx: Context, rnd: Round, folder: Path) -> str:
    gates = ", ".join(ctx.settings.pr.gates) or "none"
    lines = [
        f"# Fix the review's findings on pull request #{ctx.pr.number}",
        "",
        pragents.FRAMING,
        "",
        f"Follow the placido pr-fix skill in {pragents.SKILLS / 'pr-fix' / 'SKILL.md'}.",
        "",
        *pragents._paths_block(ctx, rnd),
        pragents.notes_line(rnd, "fix", "pr"),
        f"- **The final review:** {rnd.folder / 'final.md'}",
        f"- **The findings to fix, all of them:** {rnd.folder / 'fix-findings.json'}",
        f"- **Gates:** {gates}. Placido runs them after you, and commits only when they pass.",
        decisions.prompt_line(ctx.run.path),
        f"- **Write:** {folder / 'resolutions.json'} and the result file.",
    ]
    menu_ = config.command_menu(ctx.settings)
    return "\n".join(lines) + "\n\n" + (menu_ or "The project defines no commands.\n")


def _commit(worktree: Path, result: Path, run: runlog.Run, rnd: Round) -> str:
    text = (
        f"{implement.message(result)}\n\nPlacido-Run: {run.path.parent.name}/{run.path.name}\n"
        f"Placido-PR-Round: {rnd.number}\n"
    )
    pr.git(worktree, "add", "-A")
    done = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-F", "-"], input=text, capture_output=True, text=True
    )
    if done.returncode != 0:
        raise PRStop(f"git commit: {(done.stderr or done.stdout).strip()}")
    return pr.git(worktree, "rev-parse", "HEAD")


ACTIONS: dict[str, Callable[[Context, Round, State, Ask], None]] = {
    "edit": edit,
    "edit-merge": edit_merge,
    "fix": fix,
    "push": push,
    "post": post,
    "ci": approve_ci,
    "merge": merge,
    "close": close_pr,
}
