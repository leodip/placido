"""The end of `placido pr`: the review's summary, and the actions the user picks.

Nothing reaches GitHub or the contributor's branch until the user picks it here, and
pushing, merging, and closing ask once more (decided 2026-10-16). A merge is pinned to
the commit the review covered, or to the fixes made on it here, so nothing pushed in
between is merged unreviewed. Fixes made here go on the contributor's own branch, so
the pull request stays the one way the change lands and keeps its author.

A fix made here is checked before it is offered for pushing: the `verify` role, by
default another vendor than the fixer's, judges each finding's fix and reads the fixes
for new problems, and what it finds open goes back to the fixer, up to `[pr]
fix_rounds` times (decided 2026-10-16, after #588's fix went out checked by its gates
alone). The fixer is kept open and resumed for that, since what is left open is about
its own work; each check is a fresh session, whose value is fresh eyes. Fixes that never
passed the check can still be pushed, saying so, when the user confirms.

Before a fix, the base branch is merged into the branch when it has moved past it, as a
commit of its own, its conflicts resolved by the fix role and nothing else: the fixes
are then made, gated, and checked on what the squash merge will land, and a branch that
conflicts with the base is brought up to date without the force push a rebase needs,
which placido never does (decided 2026-10-16, after #592's review asked for a rebase).
A pull request that changes host paths is fixed on its own base instead, since the
paths placido trusts are then its approved ones.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from placido import config, decisions, fallback, github, implement, pr, pragents, prcheck, prgh, runlog, status
from placido.pr import Context, PRStop, Round

NO_CHANGES = "No code changes"
PUSH_URL = "https://github.com/{repo}.git"  # where the contributor's branch is pushed to
RESOLUTIONS = ("fixed", "disputed")
# The last gates run before the push, as in an issue run, so a failing one stops the
# push, the post, and the merge picked with it.
ORDER = ("edit", "edit-merge", "fix", "last", "push", "post", "ci", "merge", "close")
# How each verdict's comment is posted: the review's kind on GitHub, and in words.
POSTED_AS = {
    "request-changes": ("request-changes", "as a review requesting changes"),
    "merge": ("approve", "as an approving review"),
    "fix-here": ("comment", "as a comment"),
    "close": ("comment", "as a comment"),
}

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
    fixes: list[str]  # commits made here this round: the base merged in, and the fixes
    verified: bool | None  # the latest check of the fixes passed; None: never checked
    ahead: int  # commits made here that the pull request's branch lacks, the base's own not counted
    merges_base: bool  # a fix merges the base in first: it moved past the branch, which changes no host path
    pushed: bool
    posted: bool
    waiting: list[dict[str, Any]]  # CI runs held for approval
    ci: str  # green, red, pending, or none
    last: dict[str, Any] | None  # the round's latest run of the last gates


def state(ctx: Context, rnd: Round) -> State:
    number = ctx.pr.number
    pull = ctx.hub.pull_request(number)
    ctx.pr = pull
    if pull.state == "OPEN":
        pr.fetch(ctx.root, pull.base, number)
    data = final(rnd)
    run_dir = ctx.run.path
    fixes = pr.commits_here(run_dir, rnd.number)
    ahead = int(pr.git(ctx.worktree, "rev-list", "--count", "--first-parent", f"{pr.pr_ref(number)}..HEAD") or 0)
    merges_base = (
        pull.state == "OPEN" and not pr.is_ancestor(ctx.worktree, pr.base_ref(pull.base), "HEAD")
        and not pr.branch_changes_host_paths(ctx, rnd)
    )
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
        verified=(pr.done(run_dir, "pr.verified", rnd.number) or {}).get("passed"),
        ahead=ahead,
        merges_base=merges_base,
        pushed=bool(pr.round_events(run_dir, "pr.pushed", rnd.number)),
        posted=bool(pr.round_events(run_dir, "pr.posted", rnd.number)),
        waiting=waiting,
        ci=ci,
        last=pr.done(run_dir, "pr.last_gates", rnd.number),
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
        f"Verdict: {data['verdict']}{_yours(data)}. {data['summary']}",
    ]
    routing = data["routing"]
    if routing["route"] != "none":
        whom = "the contributor" if routing["route"] == "contributor" else "here, by you"
        decided = ", as you decided" if routing.get("judge_verdict") else ""
        share = routing.get("share")
        measured = (f" (about {share:.0%} of the pull request; the line is {ctx.settings.pr.contributor_share:.0%})."
                    f" {routing.get('estimate', '')}" if isinstance(share, (int, float)) else ".")
        lines.append(f"Fixes: {whom}{decided}{measured}".rstrip())
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
            decided = f" (fix: your decision {f['decided_by']})" if f.get("decided_by") and f["agreement"] != "yours" else ""
            lines.append(
                f"  {f['id']}  {f['severity']:<11} {f['area']:<11}{where} {f['title']}"
                f" [{', '.join(f['sources'])}: {f['agreement']}]{decided}"
            )
    if dropped:
        lines += ["", "Dropped:"]
        lines += [f"  {f['id']}  {f['title']} [{', '.join(f['sources'])}]: {f['reason']}" for f in dropped]
    if data.get("earlier"):
        lines += ["", "The round before:"]
        lines += [f"  {e['item']}: {e['status']}" + (f" ({e['note']})" if e.get("note") else "") for e in data["earlier"]]
    asked = prcheck.decision_items(data)
    if asked and all(item.get("decided") for item in asked):
        lines += ["", f"Your decisions (in {decisions.path(run_dir)}):"]
        for n, item in enumerate(asked, 1):
            taken = item["decided"]
            how = "as recommended" if taken["as_recommended"] else f"the recommendation was: {item['recommendation']}"
            lines.append(f"  {n}. {taken['question']} → {taken['decision']} ({how})")
    elif asked:
        lines += ["", "Decisions for you:"]
        lines += [f"  {n}. {item['question']} → {item['recommendation']}" for n, item in enumerate(asked, 1)]
    if data.get("preexisting"):
        lines += ["", "Found outside this change (for you only, never in the comment):"]
        for item in data["preexisting"]:
            flag = " [security]" if isinstance(item, dict) and item.get("security") else ""
            if isinstance(item, dict):
                where = f" ({item['location']})" if item.get("location") else ""
                lines.append(f"  - {item.get('title', '?')}{where}{flag}")
    merged = pr.done(run_dir, "pr.base_merged", rnd.number)
    if merged:
        resolved = f", resolving {', '.join(merged['conflicts'])}" if merged["conflicts"] else ""
        lines += ["", f"Merged {pull.base} ({merged['tip'][:12]}) into the branch here as {merged['commit'][:12]}{resolved}"]
    fixed = pr.round_events(run_dir, "pr.fixed", rnd.number)
    if fixed:
        last = fixed[-1]
        commit = (last.get("commit") or "")[:12] or "no commit"
        lines += ["", f"Fixed here ({commit}): {last.get('fixed', 0)} fixed, {last.get('disputed', 0)} disputed"
                  f" (see {run_dir / 'steps' / last['step'] / 'resolutions.json'})"]
        checked = pr.done(run_dir, "pr.verified", rnd.number)
        if checked:
            verdict = "passed" if checked["passed"] else f"left {', '.join(checked['open'] + checked['new'])} open"
            lines.append(f"Checked by {checked.get('agent', '?')}: {verdict}"
                         f" (see {run_dir / 'steps' / checked['step'] / 'verify.json'})")
    ran = pr.done(run_dir, "pr.last_gates", rnd.number)
    if ran:
        shown = " · ".join(f"{name} {'pass' if code == 0 else 'FAIL'}" for name, code in ran["results"].items())
        lines += ["", f"Last gates on {ran['head'][:12]}: {shown} (logs in {rnd.folder / 'last-gates'})"]
    lines += [
        "",
        f"Full review: {rnd.folder / 'final.md'}",
        f"Comment draft: {rnd.folder / 'comment.md'}",
        "",
        *comment_block(rnd),
    ]
    return "\n".join(lines) + "\n"


def _yours(data: dict[str, Any]) -> str:
    judged = data["routing"].get("judge_verdict")
    return f" (yours; the judge's was {judged})" if judged else ""


def comment_block(rnd: Round) -> list[str]:
    return [
        "---- the comment to the contributor ----",
        (rnd.folder / "comment.md").read_text(encoding="utf-8").strip(),
        "---- end of the comment ----",
    ]


# ---- the menu -------------------------------------------------------------------------------


def options(ctx: Context, rnd: Round, s: State) -> tuple[list[tuple[str, str]], list[str]]:
    """The actions open now, as (key, label), and notes on what is not."""

    if s.pull.state != "OPEN":
        # Closing removes this workspace, so close refuses to run in it.
        note = (f"The pull request is {s.pull.state.lower()}. To end the run, in {ctx.root.name}'s workspace"
                f" (not this one):\n  placido close pr-{s.pull.number}")
        return [], [note]
    out: list[tuple[str, str]] = []
    notes: list[str] = []
    out.append(("edit", f"Edit the comment ({rnd.folder / 'comment.md'})"))
    if (rnd.folder / "merge.md").is_file():
        out.append(("edit-merge", f"Edit the squash message ({rnd.folder / 'merge.md'})"))
    merge = f"Merge {s.pull.base} into the branch, then " if s.merges_base else ""
    if s.kept and not s.fixed:
        out.append(("fix", f"{merge}{'fix' if merge else 'Fix'} the {len(s.kept)} kept finding(s) here, on the"
                           " contributor's branch, and have the fixes checked: committed locally, nothing pushed"))
    elif s.fixed and s.verified is False:
        count = len(open_items(rnd))
        out.append(("fix", f"{merge}{'send' if merge else 'Send'} the {count} item(s) the check left open back to"
                           " the fixer, then check again"))
    if ctx.settings.last_gates:
        stale = f": {s.pull.base} has moved on, and this runs on the pull request's own base" if s.merges_base else ""
        out.append(("last", f"Run the last gates ({', '.join(ctx.settings.last_gates)}) on"
                            f" {pr.git(ctx.worktree, 'rev-parse', 'HEAD')[:12]}{stale}"))
    if s.ahead and s.pull.can_modify:
        target = f"{s.pull.head_repo}:{s.pull.head_ref}"
        if s.verified:
            out.append(("push", f"Push the {s.ahead} commit(s) made here, checked, to {target}"))
        elif s.verified is False:
            out.append(("push", f"Push anyway: the check left {len(open_items(rnd))} item(s) open ({target})"))
        else:
            out.append(("push", f"Push the {s.ahead} commit(s) made here, never checked, to {target}"))
    elif s.ahead:
        notes.append("The contributor does not let maintainers push to the branch, so the fixes made"
                     " here cannot be pushed from placido.")
    if not s.posted:
        out.append(("post", f"Post the comment {POSTED_AS[s.verdict][1]}"))
    if s.waiting:
        out.append(("ci", f"Approve the {len(s.waiting)} CI run(s) waiting for a maintainer"))
    if not s.ahead and (s.verdict == "merge" or s.pushed or (s.fixed and not s.fixes)):
        when = (f" once CI is green (placido waits up to {config.format_duration(ctx.settings.ci_wait)})"
                if s.ci == "pending" else "")
        out.append(("merge", f"Squash-merge into {s.pull.base}{when}, pinned to {s.pull.head_sha[:12]} (CI {s.ci})"))
    out.append(("close", "Close the pull request, posting the comment if it is not posted"))
    out.append(("quit", f"Nothing more now: placido pr {s.pull.number} shows this again"))
    return out, notes


def menu(ctx: Context, rnd: Round, read: Callable[[str], str | None], confirm: Ask) -> None:
    """Show the actions open, run the ones picked, and again, until the user is done."""

    while True:
        s = state(ctx, rnd)
        listed, notes = options(ctx, rnd, s)
        if not listed:  # settled: nothing left to pick
            print("\n" + "\n".join(notes))
            return
        print(f"\nCI {s.ci}{_last_text(ctx, s)} · {'posted' if s.posted else 'not posted'} · "
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
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"  # nano unless the user chose one
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
    """Post the comment, shown first as it goes out: a fix or an edit may have changed it
    since the summary showed it (decided 2026-10-16, after #592's comment went out unseen)."""

    kind, how = POSTED_AS[s.verdict]
    unpushed = (" It says what was fixed on the branch, and nothing is pushed yet."
                if s.verdict == "fix-here" and not s.pushed else "")
    print("\n" + "\n".join(comment_block(rnd)))
    if not confirm(f"Post this on #{s.pull.number} {how}?{unpushed}"):
        return
    ctx.hub.review(s.pull.number, kind, _body(ctx, rnd))
    ctx.run.event("pr.posted", round=rnd.number, kind=kind)
    print(f"posted on {s.pull.url}")


def _last_text(ctx: Context, s: State) -> str:
    if not ctx.settings.last_gates:
        return ""
    if s.last is None:
        return " · last gates not run"
    failed = [name for name, code in s.last["results"].items() if code]
    verdict = f"failed ({', '.join(failed)})" if failed else "passed"
    return f" · last gates {verdict} on {s.last['head'][:12]}"


def last(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    """The project's last gates, on the worktree as it is: the pull request with the fixes
    made here. A failure stops the actions picked after it."""

    results = pr.last_gates(ctx, rnd, sys.stdout)
    head = pr.git(ctx.worktree, "rev-parse", "HEAD")[:12]
    failed = [f"{name} (exit {code})" for name, code in results.items() if code]
    if failed:
        raise PRStop(f"the last gates failed on {head}: {', '.join(failed)}; logs in {rnd.folder / 'last-gates'}")
    print(f"the last gates passed on {head}")


def push(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    if not s.pull.can_modify:
        raise PRStop("the contributor does not let maintainers push to the branch")
    target = f"{s.pull.head_repo}:{s.pull.head_ref}"
    warning = {True: "", False: " The check of the fixes left items open.", None: " The fixes were never checked."}
    if not confirm(f"Push {s.ahead} commit(s) to {target}? (never forced){warning[s.verified]}"):
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
    ci, number, wait = s.ci, s.pull.number, config.format_duration(ctx.settings.ci_wait)
    if s.waiting:
        raise PRStop(f"{len(s.waiting)} CI run(s) on {head[:12]} wait for a maintainer's approval, so CI cannot"
                     " finish: approve them, with the merge if you like")
    if ci == "red":
        raise PRStop(f"CI is red on {head[:12]}; see {s.pull.url}/checks")
    if ci in ("none", "unknown") and not confirm(f"CI shows no result on {head[:12]}. Merge anyway?"):
        return
    drafted = rnd.folder / "merge.md"
    # Without the judge's draft (a request for changes fixed here instead), GitHub's own.
    text = drafted.read_text(encoding="utf-8") if drafted.is_file() else f"{s.pull.title} (#{number})"
    lines = text.strip().splitlines()
    subject = lines[0].strip()
    body = rnd.folder / "merge-body.md"
    body.write_text("\n".join(lines[1:]).strip() + "\n", encoding="utf-8")
    # Pending CI is waited out here, and the merge follows its green without asking again,
    # so the user can leave it (decided 2026-10-16).
    later = f" once CI is green on {head[:12]}? Placido waits up to {wait} and merges then" if ci == "pending" else "?"
    if not confirm(f"Squash-merge #{number} into {s.pull.base} as \"{subject}\"{later}"):
        return
    if ci == "pending":
        ctx.run.event("pr.merge_waiting", round=rnd.number, head=head)
        status.show(ctx.run.path, status.text(status.WAITING, f"CI on #{number}, then the merge"))
        ci, failed = github._await_ci(ctx.hub, number, head, ctx.settings.ci_wait, time.sleep, time.monotonic,
                                      github._progress(ctx.run, f"CI on #{number}"))
        if ci != "green":
            why = {"red": f"CI went red on {head[:12]} ({', '.join(c.get('name', '?') for c in failed)})",
                   "pending": f"CI was still running on {head[:12]} after {wait}"}.get(ci, f"CI showed no result on {head[:12]}")
            pr.notify(ctx, "not merged", f"#{number}: {why}")
            raise PRStop(f"{why}; nothing was merged. See {s.pull.url}/checks")
    ctx.hub.merge(number, head, subject, body)
    ctx.run.event("pr.merged", round=rnd.number, head=head, subject=subject)
    status.show(ctx.run.path, status.text(status.DONE, f"merged; placido close pr-{number}"))
    if later != "?":
        pr.notify(ctx, "merged", f"#{number} into {s.pull.base} once CI was green; placido close pr-{number}")
    print(f"merged #{number}")  # the menu then says how to end the run


def close_pr(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    if not s.posted:
        print("\n" + "\n".join(comment_block(rnd)))
    if not confirm(f"Close #{s.pull.number}" + ("" if s.posted else ", posting this comment") + "?"):
        return
    if not s.posted:
        ctx.hub.comment(s.pull.number, _body(ctx, rnd))
        ctx.run.event("pr.posted", round=rnd.number, kind="comment")
    ctx.hub.close(s.pull.number)
    ctx.run.event("pr.closed_pr", round=rnd.number)
    status.show(ctx.run.path, status.text(status.DONE, f"closed; placido close pr-{s.pull.number}"))
    print(f"closed #{s.pull.number}")


# ---- fixing it here ---------------------------------------------------------------------------


def open_items(rnd: Round) -> list[dict[str, Any]]:
    """What the latest check of the fixes left open, for the next fix."""

    path = rnd.folder / "open-items.json"
    return json.loads(path.read_text(encoding="utf-8"))["findings"] if path.is_file() else []


def fix(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    """Merge the base in when it moved on, fix the kept findings here, on the
    contributor's branch, then have the fixes checked; what the check leaves open goes
    back to the fixer, up to `[pr] fix_rounds` times. Placido runs the gates and
    commits; nothing is pushed."""

    items = open_items(rnd) if s.fixed and s.verified is False else s.kept
    done_before = len(pr.round_events(ctx.run.path, "pr.fixed", rnd.number))
    ctx.run.event("pr.fix_start", round=rnd.number, findings=[f["id"] for f in items])
    before = pr.git(ctx.worktree, "rev-parse", "HEAD")  # the first check reads the merge too
    merge_base_in(ctx, rnd)
    try:
        for n in range(1, ctx.settings.pr.fix_rounds + 2):
            attempt_no = done_before + n
            folder = fix_once(ctx, rnd, items, attempt_no)
            data = check_fix(ctx, rnd, items, attempt_no, before, folder)
            if prcheck.verify_passed(data):
                print(f"the check of fix {attempt_no} passed: every finding is resolved, and nothing new is serious")
                return
            items = _still_open(data, items, attempt_no)
            (rnd.folder / "open-items.json").write_text(json.dumps({"findings": items}, indent=2) + "\n",
                                                        encoding="utf-8")
            print(f"the check of fix {attempt_no} left {len(items)} item(s) open: "
                  + ", ".join(f"{i['id']} {i['title']}" for i in items))
            before = pr.git(ctx.worktree, "rev-parse", "HEAD")
        print("No fix round is left: push anyway, or send them back to the fixer from the menu.")
    finally:
        _close_fixer(ctx)


# ---- the base merged in before a fix ---------------------------------------------------

# A line git writes around a conflict; `=======` alone is left out, being a Markdown
# heading's underline too.
MARKER = re.compile(r"^(<{7}|>{7}|\|{7})( |$)", re.MULTILINE)


def merge_base_in(ctx: Context, rnd: Round) -> None:
    """Merge the base branch into the branch, as a commit of its own, when it has moved
    past it and the pull request changes no host path. A conflict goes to the fix role,
    which resolves the conflicts and nothing else; the gates run with the fix after."""

    wt, run, base = ctx.worktree, ctx.run, ctx.pr.base
    tip = pr.git(wt, "rev-parse", pr.base_ref(base))
    if pr.is_ancestor(wt, tip, "HEAD"):
        return
    if pr.branch_changes_host_paths(ctx, rnd):
        run.event("pr.base_merge_skipped", round=rnd.number, tip=tip, reason="host paths")
        print(f"{base} has moved on, but the branch changes host paths, so placido does not merge"
              f" {base} into it: the fixes are made on its own base.")
        return
    pr.verify_host_paths(ctx, rnd, f"the merge of {base}")
    if pr.git(wt, "status", "--porcelain"):  # a merge that fails is undone with a hard reset
        raise PRStop(f"{wt} has uncommitted changes; look at them (git -C {wt} status) and clean up first")
    head = pr.git(wt, "rev-parse", "HEAD")
    status.show(run.path, status.text(status.WORKING, f"merging {base}"))
    done = subprocess.run(
        ["git", "-C", str(wt), "merge", "-q", "--no-ff", "--no-commit", tip], capture_output=True, text=True,
    )
    conflicts = pr.git(wt, "diff", "--name-only", "--diff-filter=U").splitlines()
    if done.returncode != 0 and not conflicts:
        _undo_merge(ctx, rnd, head)
        raise PRStop(f"git merge {base}: {(done.stderr or done.stdout).strip()[-400:]}")
    if conflicts:
        resolve_conflicts(ctx, rnd, tip, head, conflicts)
    commit = _commit(wt, f"Merge branch '{base}' into {ctx.pr.head_ref}", run, rnd)
    run.event("pr.base_merged", round=rnd.number, commit=commit, tip=tip, conflicts=conflicts)
    print(f"merged {base} ({tip[:12]}) into the branch as {commit[:12]}"
          + (f", resolving {', '.join(conflicts)}" if conflicts else "") + "; not pushed")


def resolve_conflicts(ctx: Context, rnd: Round, tip: str, head: str, conflicts: list[str]) -> None:
    """The fix role resolves a merge's conflicts and leaves the merge for placido to
    commit; one that does not finish leaves nothing behind but what it did, as evidence."""

    run, wt = ctx.run, ctx.worktree
    created = set(_untracked(wt))  # what git wrote itself, such as both sides of a rename
    merged = _merged_entries(wt)
    chain = fallback.Chain(
        run, "fix", fallback.entries(ctx.settings, "fix"), lambda title, body: pr.notify(ctx, title, body),
        since=lambda event: event.get("event") == "pr.fix_start",
    )

    def check(result: Path) -> list[str]:
        if implement.blocked(result) is not None:
            return []
        return decisions.check(result.parent, run.path) + merge_problems(ctx, tip, head, conflicts, created, merged)

    name = f"r{rnd.number}-merge"

    def start(spec: config.AgentSpec) -> Any:
        folder = run.new_step_dir(name)
        return ctx.runner(
            "fix", spec, merge_prompt(ctx, rnd, tip, conflicts), name=name, folder=folder, check=check,
            ask=implement.blocked,
        )

    step = pragents.attempt(ctx, chain, start)
    if step.outcome != "success":
        evidence = _undo_merge(ctx, rnd, head)
        raise PRStop(
            f"the merge of {ctx.pr.base} did not finish ({step.outcome}); nothing is committed, and what was"
            f" done is in {evidence}. See {step.path}"
        )


def merge_problems(
    ctx: Context, tip: str, head: str, conflicts: list[str], created: set[str], merged: dict[str, str],
) -> list[str]:
    """What is wrong with a resolution: the merge no longer in progress, a conflict
    marker left, a change beyond the conflicts, a host path not as the base has it."""

    wt, base = ctx.worktree, ctx.pr.base
    if pr.git(wt, "rev-parse", "HEAD") != head or _merge_head(wt) != tip:
        return [
            "the merge is no longer in progress, and placido commits it. Put it back: `git merge --abort` if"
            f" one is open, `git reset -q --hard {head[:12]}`, `git merge --no-ff --no-commit {tip[:12]}`;"
            " then resolve the conflicts again."
        ]
    problems = []
    marked = [name for name in conflicts if MARKER.search(_read(wt / name))]
    if marked:
        problems.append(f"conflict markers are left in {', '.join(marked)}.")
    now = _merged_entries(wt)
    staged = {p for p in set(merged) | set(now) if merged.get(p) != now.get(p)}
    loose = set(pr.git(wt, "diff", "--name-only").splitlines()) | (set(_untracked(wt)) - created)
    beyond = sorted((staged | loose) - set(conflicts))
    if beyond:
        problems.append(
            f"only the conflicts: {', '.join(beyond)} merged cleanly, and you changed them. Put them back"
            " (`git checkout -- <file>`; `git rm --cached` and delete a file you added). The findings are"
            " fixed after the merge, in a commit of their own."
        )
    host = pr.host_paths_differ(wt, tip, ctx.settings.pr.host_paths)
    if host:
        problems.append(
            f"{', '.join(host)} run on this machine and must stay as {base} has them:"
            f" `git checkout {tip[:12]} -- <file>`."
        )
    return problems


def merge_prompt(ctx: Context, rnd: Round, tip: str, conflicts: list[str]) -> str:
    base = ctx.pr.base
    lines = [
        f"# Resolve the conflicts of merging {base} into pull request #{ctx.pr.number}",
        "",
        pragents.FRAMING,
        "",
        f"Follow the placido pr-fix skill in {pragents.SKILLS / 'pr-fix' / 'SKILL.md'}: its section on"
        " conflicts with the base.",
        "",
        *pragents._paths_block(ctx, rnd),
        pragents.notes_line(rnd, "fix", "pr"),
        f"- **The final review:** {rnd.folder / 'final.md'}",
        f"- **The merge:** placido is merging {base} ({tip[:12]}) into the branch, so that the fixes are made"
        f" on what will land, and git stopped on conflicts in: {', '.join(conflicts)}.",
        "- **Write:** the result file: a line per file, how you resolved it.",
    ]
    menu_ = config.command_menu(ctx.settings)
    return "\n".join(lines) + "\n\n" + (menu_ or "The project defines no commands.\n")


def _merged_entries(wt: Path) -> dict[str, str]:
    """The index's merged entries, path to object: what the merge left staged."""

    entries = {}
    for line in pr.git(wt, "ls-files", "--stage").splitlines():
        meta, path = line.split("\t", 1)
        _mode, oid, stage = meta.split()
        if stage == "0":
            entries[path] = oid
    return entries


def _merge_head(wt: Path) -> str:
    """The commit being merged in, or "" when no merge is in progress."""

    done = subprocess.run(["git", "-C", str(wt), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True, text=True)
    return done.stdout.strip()


def _untracked(wt: Path) -> list[str]:
    return pr.git(wt, "ls-files", "--others", "--exclude-standard").splitlines()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""  # deleted: a resolution too


def _undo_merge(ctx: Context, rnd: Round, head: str) -> Path:
    """Put the worktree back at head, clean, as it was before the merge, keeping what was
    there as evidence. A hard reset rather than `git merge --abort`, which refuses a file
    the merge added and the resolver then edited, and cannot undo a resolver's commit."""

    wt = ctx.worktree
    evidence = rnd.folder / "merge-unfinished.diff"
    text = pr.git(wt, "status", "--short", "--untracked-files=all") + "\n\n" + pr.git(wt, "diff") + "\n"
    for name in _untracked(wt):
        text += f"\n--- untracked {name}\n{_read(wt / name)}"
    evidence.write_text(text, encoding="utf-8")
    pr.git(wt, "reset", "-q", "--hard", head)
    pr.git(wt, "clean", "-fdq")
    ctx.run.event("pr.base_merge_undone", round=rnd.number, evidence=evidence.name)
    return evidence


def fix_once(ctx: Context, rnd: Round, items: list[dict[str, Any]], attempt_no: int) -> Path:
    """One fix: the fix role answers every item, placido runs the gates and commits.
    Returns the fix's step folder."""

    run, wt = ctx.run, ctx.worktree
    ids = [f["id"] for f in items]
    listed = rnd.folder / f"fix-findings-{attempt_no}.json"
    listed.write_text(json.dumps({"findings": items}, indent=2) + "\n", encoding="utf-8")
    head = pr.git(wt, "rev-parse", "HEAD")
    pr.verify_host_paths(ctx, rnd, "the fix")
    chain = fallback.Chain(
        run, "fix", fallback.entries(ctx.settings, "fix"), lambda title, body: pr.notify(ctx, title, body),
        since=lambda event: event.get("event") == "pr.fix_start",
    )
    entry = {"id": f"pr-{ctx.pr.number}", "title": "review fixes", "gates": list(ctx.settings.pr.gates)}
    settings = replace(ctx.settings, mutations=0)

    def check(result: Path) -> list[str]:
        if implement.blocked(result) is not None:
            return []
        problems = decisions.check(result.parent, run.path) or check_resolutions(result.parent / "resolutions.json", ids)
        if problems:
            return problems
        host = pr.changed_files(wt, *ctx.settings.pr.host_paths) if ctx.settings.pr.host_paths else []
        if host:
            # Asked before the gates below run any of it.
            answer = pr.approve_host_change(ctx, rnd, f"fix {attempt_no}")
            if not answer:
                who = "the user refused" if answer is False else "nobody was at the terminal to approve"
                return [f"{who} your change to {', '.join(host)}, which run on this machine; undo it,"
                        " and dispute the findings that needed it, saying so."]
        if result.read_text(encoding="utf-8").strip().splitlines()[:1] == [NO_CHANGES]:
            return [f"the result says '{NO_CHANGES}', but the worktree has changes."] if pr.git(wt, "status", "--porcelain") else []
        return implement.check(result, wt, head, entry, settings, ctx.env, run)

    name = f"r{rnd.number}-fix-{attempt_no}"

    def start(spec: config.AgentSpec) -> Any:
        folder = run.new_step_dir(name)
        return ctx.runner(
            "fix", spec, fix_prompt(ctx, rnd, listed, folder), name=name, folder=folder, check=check,
            keep_open=True, ask=implement.blocked,
        )

    kept, step = _kept_fixer(ctx), None
    if kept:
        folder = run.new_step_dir(name)
        step = ctx.runner.resume(
            "fix", kept["kind"], kept["name"], kept["pane"], fix_prompt(ctx, rnd, listed, folder, resumed=True),
            name=name, folder=folder, check=check, keep_open=True, ask=implement.blocked,
        )
        if step.outcome != "success":
            _close_fixer(ctx)
    if step is None or step.outcome != "success":
        step = pragents.attempt(ctx, chain, start)
        kept = None
    if step.outcome != "success":
        if pr.git(wt, "status", "--porcelain"):
            message = f"placido: pr fix failed ({step.outcome}, {run.path.name})"
            pr.git(wt, "stash", "push", "--include-untracked", "-m", message)
            run.event("worktree.stashed", message=message)
        raise PRStop(f"the fix did not finish ({step.outcome}); see {step.path}")
    # Kept open from here, so that the end of the fix closes it whatever stops it next.
    run.event(
        "pr.kept_open", round=rnd.number, role="fix", step=step.path.name, name=kept["name"] if kept else step.agent,
        pane=kept["pane"] if kept else step.pane, kind=kept["kind"] if kept else chain.current.agent,
    )
    data = json.loads((step.path / "resolutions.json").read_text(encoding="utf-8"))
    counts = {k: sum(1 for r in data["resolutions"] if r.get("status") == k) for k in RESOLUTIONS}
    result = step.path / "result.md"
    commit = None
    if result.read_text(encoding="utf-8").strip().splitlines()[:1] != [NO_CHANGES]:
        # The gates in the check ran the pull request's tests after the fixer's own check
        # of the host paths: whatever they wrote there must not ride into the commit.
        pr.verify_host_paths(ctx, rnd, "the fix's commit")
        approved = pr.untrusted_host_files(ctx, rnd, pr.primary_lane(ctx))
        commit = _commit(wt, implement.message(result), run, rnd)
        if approved:
            run.event("pr.host_change_committed", round=rnd.number, commit=commit, files=approved)
    run.event("pr.fixed", round=rnd.number, attempt=attempt_no, commit=commit, step=step.path.name, **counts)
    pr.settle(ctx, rnd, f"fix {attempt_no}")
    print(f"\nfix {attempt_no}: {counts['fixed']} fixed, {counts['disputed']} disputed"
          + (f"; committed as {commit[:12]}, not pushed" if commit else "; no code changed"))
    if counts["disputed"]:
        print("The comment may name findings the fix disputed: edit it before posting.")
    return step.path


def _kept_fixer(ctx: Context) -> dict[str, Any] | None:
    """The fixer kept open after its last fix: a later fix resumes it, since what the
    check left open is about the fixer's own work (decided 2026-10-16). Only the
    round's can be open: a new round closes every agent kept open."""

    kept = [a for a in pr.open_agents(ctx.run.path) if a.get("role") == "fix"]
    return kept[-1] if kept else None


def _close_fixer(ctx: Context) -> None:
    kept = _kept_fixer(ctx)
    if kept:
        ctx.runner.close_agent(kept["name"], kept["pane"], ctx.run.path / "steps" / kept["step"], kept["kind"])
        ctx.run.event("pr.agent_closed", name=kept["name"])


def check_fix(
    ctx: Context, rnd: Round, items: list[dict[str, Any]], attempt_no: int, before: str, fix_folder: Path,
) -> dict[str, Any]:
    """The `verify` role checks one fix: each item's answer, and the fixes themselves."""

    run = ctx.run
    ids = [f["id"] for f in items]
    chain = fallback.Chain(
        run, "verify", fallback.entries(ctx.settings, "verify"), lambda title, body: pr.notify(ctx, title, body),
        since=lambda event: event.get("event") == "pr.fix_start",
    )
    name = f"r{rnd.number}-verify-{attempt_no}"
    listed = rnd.folder / f"fix-findings-{attempt_no}.json"

    def start(spec: config.AgentSpec) -> Any:
        folder = run.new_step_dir(name)
        return ctx.runner(
            "verify", spec, verify_prompt(ctx, rnd, listed, fix_folder, before, folder), name=name, folder=folder,
            check=lambda r: prcheck.check_verify(r.parent / "verify.json", ids),
        )

    step = pragents.attempt(ctx, chain, start)
    if step.outcome != "success":
        raise PRStop(f"the check of fix {attempt_no} did not finish ({step.outcome}); see {step.path}")
    data = json.loads((step.path / "verify.json").read_text(encoding="utf-8"))
    still = [v["id"] for v in data["verdicts"] if v["verdict"] in prcheck.STILL_OPEN]
    run.event(
        "pr.verified", round=rnd.number, attempt=attempt_no, passed=prcheck.verify_passed(data), open=still,
        new=[f["id"] for f in data.get("findings", [])], step=step.path.name, agent=chain.current.agent,
    )
    pr.settle(ctx, rnd, f"the check of fix {attempt_no}")
    return data


def _still_open(data: dict[str, Any], items: list[dict[str, Any]], attempt_no: int) -> list[dict[str, Any]]:
    """The next fix's items: those the check left open, with its note, then the new
    problems it found that are not minor, renamed so ids stay unique across fixes."""

    notes = {v["id"]: v for v in data["verdicts"]}
    still = [
        {**item, "check": f"{notes[item['id']]['verdict']}: {notes[item['id']]['note']}"}
        for item in items if notes.get(item["id"], {}).get("verdict") in prcheck.STILL_OPEN
    ]
    new = [
        {**finding, "id": f"V{attempt_no}-{finding['id'][1:]}"}
        for finding in data.get("findings", []) if finding.get("severity") != "minor"
    ]
    return still + new


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


def fix_prompt(ctx: Context, rnd: Round, listed: Path, folder: Path, resumed: bool = False) -> str:
    """The fixer's task. A resumed fixer has the framing, the skill, and the paths from its
    first fix, and is told what the check of its own work found instead."""

    if resumed:
        lines = [
            f"# The check of your fix to pull request #{ctx.pr.number} left items open",
            "",
            "Placido committed your fix, and another agent checked it: what it found still wrong, or"
            " new, is in the findings file below. Answer each as before, following the same skill.",
            "",
        ]
    else:
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
        ]
    lines.append(
        f"- **The findings to fix, all of them:** {listed}. An item with a `check` names what the"
        " check of an earlier fix still found wrong."
    )
    merged = pr.done(ctx.run.path, "pr.base_merged", rnd.number)
    if merged and not resumed:
        lines.append(
            f"- **The base:** placido merged {ctx.pr.base} ({merged['tip'][:12]}) into the branch before you, as"
            f" {merged['commit'][:12]}, so you fix what will land (`git -C {ctx.worktree} show --remerge-diff"
            f" {merged['commit'][:12]}` shows how its conflicts were resolved). A finding that asks for a rebase"
            " or a merge is answered by it: fix what remains, such as tests the base's changes broke."
        )
    gates = ", ".join(ctx.settings.pr.gates) or "none"
    host = ", ".join(ctx.settings.pr.host_paths) or "none"
    lines += [
        f"- **Paths that run on this machine:** {host}. Change them only when a finding needs it:"
        " placido shows the change to the user, who approves it before anything runs it.",
        f"- **Gates:** {gates}. Placido runs them after you and commits only when they pass, so"
        " run only the narrow tests for what you change.",
        decisions.prompt_line(ctx.run.path, pragents.OVER),
        f"- **Write:** {folder / 'resolutions.json'} and the result file.",
    ]
    menu_ = config.command_menu(ctx.settings)
    return "\n".join(lines) + "\n\n" + (menu_ or "The project defines no commands.\n")


def verify_prompt(ctx: Context, rnd: Round, listed: Path, fix_folder: Path, before: str, folder: Path) -> str:
    head = pr.git(ctx.worktree, "rev-parse", "HEAD")
    fixes = (f"`git -C {ctx.worktree} log -p --first-parent --remerge-diff {before[:12]}..{head[:12]}`"
             f" (a merge of {ctx.pr.base} shows only what its conflicts were resolved to)" if head != before
             else "none: the fixer changed no code, disputing every item")
    lines = [
        f"# Check the fixes to pull request #{ctx.pr.number}",
        "",
        pragents.FRAMING,
        "",
        f"Follow the placido pr-verify skill in {pragents.SKILLS / 'pr-verify' / 'SKILL.md'}.",
        "",
        *pragents._paths_block(ctx, rnd),
        pragents.notes_line(rnd, "review", "pr"),
        f"- **The final review:** {rnd.folder / 'final.md'}",
        f"- **The findings this fix answered:** {listed}",
        f"- **The fixer's answers:** {fix_folder / 'resolutions.json'}",
        f"- **The fixes:** {fixes}. The worktree is at {head[:12]}; leave it so, with nothing"
        " changed or added: placido checks.",
        decisions.prompt_line(ctx.run.path, pragents.OVER),
        "- **Mutations:** `placido mutate <file> --replace \"<old>\" --with \"<new>\" -- <narrow test"
        " command>`, which restores the file however it ends.",
        f"- **Write:** {folder / 'verify.json'} (new problems as V1, V2, …).",
    ]
    menu_ = config.command_menu(ctx.settings)
    return "\n".join(lines) + "\n\n" + (menu_ or "The project defines no commands.\n")


def _commit(worktree: Path, message: str, run: runlog.Run, rnd: Round) -> str:
    text = f"{message}\n\nPlacido-Run: {run.path.parent.name}/{run.path.name}\nPlacido-PR-Round: {rnd.number}\n"
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
    "last": last,
    "push": push,
    "post": post,
    "ci": approve_ci,
    "merge": merge,
    "close": close_pr,
}
