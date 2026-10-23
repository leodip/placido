"""The end of `placido pr`: the review's summary, and the actions the user picks.

Nothing reaches GitHub or the contributor's branch until the user picks it here, and
pushing and merging ask once more (decided 2026-10-16). The menu offers no editing of
the comment or the squash message, nor closing the pull request: the user never picked
them (decided 2026-10-22). Fixes made here go on
the contributor's own branch, so the pull request stays the one way the change lands
and keeps its author. Whatever the review finds, the maintainer fixes it here: nothing
goes back to the contributor, whose comment says the change is good, or what was
changed on their branch (decided 2026-10-17).

Every change made here is reviewed in full before it can go out. Once a fix is
committed, a new round reviews the branch with it: every reviewer, the cross-checks,
and the judge, as the contributor's own head was. What that round keeps is fixed in
turn, and reviewed again, up to `[pr] fix_rounds` times before the menu asks again.
So the worktree is pushed, and the pull request merged, only at the head the latest
round reviewed, with nothing left from its review but minor findings (decided
2026-10-17: #611's fixes went out read by one agent alone, the `verify` role, which
this replaces). The user's own commits in the worktree, or an agent's they ran there,
are trusted: they go out with that head, on top of it, and start no round (decided
2026-10-21: "if the commit is mine it's safe", after #619's merge waited on one).

Before a fix, the base branch is merged into the branch when it has moved past it, as a
commit of its own, its conflicts resolved by the fix role and nothing else: the fixes
are then made, gated, and reviewed on what the squash merge will land, and a branch that
conflicts with the base is brought up to date without the force push a rebase needs,
which placido never does (decided 2026-10-16, after #592's review asked for a rebase).
A pull request that changes host paths is fixed on its own base instead, since the
paths placido trusts are then its approved ones.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from placido import config, decisions, fallback, github, implement, pr, pragents, prcheck, prgh, runlog, status, step
from placido.herdr import HerdrError
from placido.pr import Context, PRStop, Round

NO_CHANGES = "No code changes"
PUSH_URL = "https://github.com/{repo}.git"  # where the contributor's branch is pushed to
RESOLUTIONS = ("fixed", "disputed")
# The last gates run before the push, as in an issue run, so a failing one stops the
# push, the post, and the merge picked with it.
ORDER = ("fix", "review", "last", "push", "post", "ci", "merge", "ask")
# How long a push waits for GitHub to show the pull request at the commit pushed: it takes
# seconds, and a menu read before then offered the same push again (#630, 2026-10-22).
PUSH_SHOWN = 60
# How each verdict's comment is posted: the review's kind on GitHub, and in words.
POSTED_AS = {
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
    kept: list[dict[str, Any]]
    fixed: bool  # a fix ran this round, whether or not it changed code: a new round reviews it
    yours: list[str]  # the user's own commits in the worktree this round: trusted, no round reviews them
    local: str  # the worktree's head
    outgoing: str  # what push and merge take: the round's head with only the user's commits on it, else ""
    unpushed: list[str]  # the commits made here that the pull request's branch lacks, newest first, as "sha subject"
    ahead: int  # how many: the base's own, merged in, not counted
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
    run_dir, wt = ctx.run.path, ctx.worktree
    local = pr.git(wt, "rev-parse", "HEAD")
    pr.adopt(ctx, rnd, local)  # so a fix goes on from them, not over them
    expected = pr.expected_head(ctx, rnd)
    taken = [c for e in pr.round_events(run_dir, "pr.adopted", rnd.number) for c in e.get("commits", [])]
    since = (pr.git(wt, "rev-list", "--reverse", f"{expected}..{local}").splitlines()
             if local != expected and pr.is_ancestor(wt, expected, local) else [])
    above = pr.git(wt, "rev-list", f"{rnd.head}..{local}").splitlines() if pr.is_ancestor(wt, rnd.head, local) else None
    unpushed = pr.git(wt, "log", "--first-parent", "--format=%H %s", f"{pr.pr_ref(number)}..HEAD").splitlines()
    merges_base = (
        pull.state == "OPEN" and not pr.is_ancestor(wt, pr.base_ref(pull.base), "HEAD")
        and not pr.branch_changes_host_paths(ctx, rnd)
    )
    try:
        waiting = ctx.hub.waiting_runs(pull.head_sha) if pull.state == "OPEN" else []
        ci = prgh.ci_state(ctx.hub.checks(number)) if pull.state == "OPEN" else "none"
    except prgh.PRError:
        waiting, ci = [], "unknown"
    return State(
        pull=pull,
        verdict=prcheck.verdict_of(data),
        kept=[f for f in data["findings"] if f["decision"] == "keep"],
        fixed=bool(pr.round_events(run_dir, "pr.fixed", rnd.number)),
        yours=taken + since,
        local=local,
        outgoing=local if above is not None and set(above) <= set(taken + since) else "",
        unpushed=unpushed,
        ahead=len(unpushed),
        merges_base=merges_base,
        pushed=bool(pr.round_events(run_dir, "pr.pushed", rnd.number)),
        posted=bool(pr.round_events(run_dir, "pr.posted", rnd.number)),
        waiting=waiting,
        ci=ci,
        last=pr.done(run_dir, "pr.last_gates", rnd.number),
    )


def blocked(rnd: Round, s: State) -> str:
    """Why the worktree cannot be pushed, nor the pull request merged, now; "" when it
    can: at the head the round reviewed in full, with only the user's own commits on
    it, and nothing left from its review but minor findings."""

    if s.fixed:
        return f"the changes made here since round {rnd.number}'s review wait for a full review of their own"
    if not s.outgoing:
        return (f"the worktree is at {s.local[:12]}, not at {rnd.head[:12]}, the head round {rnd.number} reviewed,"
                " nor on it with only your own commits")
    if s.verdict == "close":
        return f"round {rnd.number}'s verdict is close"
    serious = [f["id"] for f in s.kept if f.get("severity") != "minor"]
    if serious:
        return f"round {rnd.number} kept {', '.join(serious)}, which are not minor: fix them first"
    return ""


def _trusted(s: State) -> str:
    return f", with your {len(s.yours)} own commit(s) on it, trusted" if s.yours else ""


# ---- the summary --------------------------------------------------------------------------


def summary(ctx: Context, rnd: Round) -> str:
    data, run_dir, pull = final(rnd), ctx.run.path, ctx.pr
    lines = [
        f"# Pull request #{pull.number}: {pull.title}",
        "",
        f"{pull.url} · @{pull.author} · +{pull.additions} −{pull.deletions} in {pull.files} files"
        f" · round {rnd.number}, head {rnd.head[:12]}",
        "",
        f"Verdict: {prcheck.verdict_of(data)}{_yours(data)}. {data['summary']}",
    ]
    first = pr.contributor_round(run_dir, rnd)
    if rnd.here:
        made = pr.git(ctx.worktree, "rev-list", "--count", "--first-parent", f"{first.head}..{rnd.head}")
        lines.append(f"Reviewed here: the branch with the {made} commit(s) made here since round {rnd.previous}'s"
                     f" review, on {first.head[:12]}, the contributor's head")
    scanned = pr.done(run_dir, "pr.scan", first.number) or {}
    scan_text = "clear" if scanned.get("verdict") == "clear" else f"{scanned.get('concerns', '?')} concern(s), accepted by you"
    lines.append(f"Scan: {scan_text}" + (f" (round {first.number}'s)" if first.number != rnd.number else ""))
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
                  f" (see {run_dir / 'steps' / last['step'] / 'resolutions.json'}); not reviewed yet"]
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
    judged = prcheck.judge_verdict(data)
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
    wt, rounds = ctx.worktree, ctx.settings.pr.fix_rounds
    if final(rnd).get("verdict") == "request-changes":
        notes.append("This review is from before placido stopped asking contributors for changes, and its comment"
                     " asks the contributor for them: fix them here, and the next round writes one that says what"
                     " you changed.")
    if s.yours:
        notes.append(f"Your {len(s.yours)} commit(s) in the worktree, up to {s.yours[-1][:12]}, are trusted:"
                     f" they go out with the head round {rnd.number} reviewed.")
    if pr.git(wt, "status", "--porcelain"):
        notes.append(f"The worktree has uncommitted changes: commit them to have them go out, or drop them"
                     f" (git -C {wt} status).")
    merge = f"Merge {s.pull.base} into the branch, then " if s.merges_base else ""
    if s.fixed:
        out.append(("review", f"Review the branch with the fix in full, in round {rnd.number + 1}: every reviewer,"
                              " the cross-checks, and the judge"))
    elif s.kept and s.verdict == "fix-here":
        out.append(("fix", f"{merge}{'fix' if merge else 'Fix'} the {len(s.kept)} kept finding(s) here, on the"
                           " contributor's branch, then review the branch with the fixes in full, in a new round;"
                           f" what it keeps is fixed and reviewed in turn, up to {rounds} times. Committed locally,"
                           " nothing pushed"))
    if ctx.settings.last_gates:
        stale = f": {s.pull.base} has moved on, and this runs on the pull request's own base" if s.merges_base else ""
        out.append(("last", f"Run the last gates ({', '.join(ctx.settings.last_gates)}) on {s.local[:12]}{stale}"))
    why = blocked(rnd, s)
    minor = f", leaving {len(s.kept)} minor finding(s)" if s.kept else ""
    if s.ahead and s.pull.can_modify and not why:
        target = f"{s.pull.head_repo}:{s.pull.head_ref}"
        out.append(("push", f"Push the {s.ahead} commit(s) made here, reviewed in full in round {rnd.number}"
                            f"{_trusted(s)}{minor}, to {target}"))
    elif s.ahead and not why:
        notes.append("The contributor does not let maintainers push to the branch, so the fixes made"
                     " here cannot be pushed from placido.")
    if not s.posted:
        out.append(("post", f"Post the comment {POSTED_AS[s.verdict][1]}"))
    if s.waiting:
        out.append(("ci", f"Approve the {len(s.waiting)} CI run(s) waiting for a maintainer"))
    if not s.ahead and not why and s.pull.head_sha == s.outgoing:
        when = (f" once CI is green (placido waits up to {config.format_duration(ctx.settings.ci_wait)})"
                if s.ci == "pending" else "")
        out.append(("merge", f"Squash-merge into {s.pull.base}{when}, pinned to {s.pull.head_sha[:12]}"
                             f" (CI {s.ci}){minor}"))
    elif not s.ahead and not why:
        notes.append(f"The pull request's head is {s.pull.head_sha[:12]}, which no round reviewed:"
                     f" placido pr {s.pull.number} reviews it.")
    asked = to_ask(ctx, rnd)
    if asked is not None:
        out.append(("ask", f"Ask {_who(asked)} questions, read-only, in a new tab"))
    out.append(("quit", f"Nothing more now: placido pr {s.pull.number} shows this again"))
    return out, notes


def menu(ctx: Context, rnd: Round, read: Callable[[str], str | None], confirm: Ask) -> None:
    """Show the actions open, run the ones picked, and again, until the user is done. An
    action that ends in a new round, a fix or a review, goes on with that round, and
    the ones picked after it wait for the next menu, which reads it."""

    while True:
        s = state(ctx, rnd)
        listed, notes = options(ctx, rnd, s)
        if not listed:  # settled: nothing left to pick
            print("\n" + "\n".join(notes))
            return
        print(f"\nCI {s.ci}{_last_text(ctx, s)} · {'posted' if s.posted else 'not posted'} · "
              f"{len(s.waiting)} CI run(s) waiting")
        print(_unpushed_text(s))
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
        for key in [k for k in ORDER if k in picked]:
            # As things are now, not as the menu showed them: the answer can come long
            # after, with a commit or a push made meanwhile, as #619's merge was picked
            # after the ask tab pushed a commit (2026-10-21).
            s = state(ctx, rnd)
            try:
                reviewed = ACTIONS[key](ctx, rnd, s, confirm)
            except (PRStop, prgh.PRError, implement.ImplementError) as error:
                print(f"placido: {key}: {error}")
                ctx.run.event("pr.action_failed", round=rnd.number, action=key, error=str(error)[:500])
                # A fix goes on through rounds of its own; one that stops in a later round
                # leaves the menu there. On the round it began in, the menu took the later
                # rounds' commits, main's merged in among them, for the user's own (#628).
                rnd = pr.latest_judged(ctx.run.path) or rnd
                break
            if isinstance(reviewed, Round):
                rnd = reviewed
                print("\n" + summary(ctx, rnd))
                break


def _unpushed_text(s: State, shown: int = 10) -> str:
    """The commits made here that the pull request's branch lacks, one a line."""

    target = f"{s.pull.head_repo}:{s.pull.head_ref}"
    if not s.unpushed:
        return f"Not pushed: nothing; {target} has every commit here"
    lines = [f"Not pushed: {len(s.unpushed)} commit(s) here that {target} lacks"]
    for line in s.unpushed[:shown]:
        sha, _, subject = line.partition(" ")
        lines.append(f"  {sha[:12]} {subject}")
    if len(s.unpushed) > shown:
        lines.append(f"  and {len(s.unpushed) - shown} more")
    return "\n".join(lines)


# ---- questions to an agent ------------------------------------------------------------

# Whose session the user's questions go to: the round's last fixer, about its fixes, else
# its judge, about the decision. The session is copied, so the one placido may resume
# stays as it was, and the copy starts read-only, since a change made there skips the
# gates a fix gets; the user can lift that in the agent, and what it commits is the
# user's, trusted as their own commits are (decided 2026-10-21). With neither session at
# hand, a new agent of the fix role's is asked (decided 2026-10-17).
ASKED = {"fix": "the fixer", "judge": "the judge"}
FORK = {"claude": ["--resume", "{id}", "--fork-session"], "codex": ["fork", "{id}"]}
READ_ONLY = {"claude": ["--permission-mode", "plan"], "codex": ["-s", "read-only", "-a", "on-request"]}
ASK_CHANGE = (
    " If they ask you for a change after all, commit it: placido takes it as theirs, and pushes it with the"
    " rest from its menu. Push it yourself only when they ask you to."
)
ASK_COPY = ("From here on the maintainer asks you about your work on pull request #{number}. Answer; change nothing."
            + ASK_CHANGE)
ASK_NEW = (
    "The maintainer has questions about pull request #{number}, whose branch is checked out here. Placido"
    " reviewed it: {folder} holds the review, final.json its decision and findings, comment.md the comment"
    " to the contributor. Answer their questions; change nothing." + ASK_CHANGE
)


@dataclass(frozen=True)
class Asked:
    spec: config.AgentSpec
    role: str = ""  # the step role whose session is copied; empty for a new agent
    session: str = ""


def _who(asked: Asked) -> str:
    if asked.role:
        return f"{ASKED[asked.role]} ({asked.spec.agent}, a copy of its session)"
    return f"a new agent ({asked.spec.agent}, which knows only the review's files)"


def to_ask(ctx: Context, rnd: Round) -> Asked | None:
    """The agent the user's questions go to, as ASKED says; None when the fix role has
    no agent that can be asked."""

    roles: dict[str, str] = {}
    started: dict[str, dict[str, Any]] = {}
    found: dict[str, Asked] = {}
    for event in runlog.read_events(ctx.run.path):
        kind, name = event.get("event"), str(event.get("step", ""))
        if kind == "step.start":
            roles[name] = str(event.get("role", ""))
        elif kind == "agent.start":
            started[name] = event
        elif (kind == "agent.session" and roles.get(name) in ASKED and f"-r{rnd.number}-" in name
              and event.get("agent") in FORK and event.get("kind") == "id"):
            role, value, start = roles[name], str(event.get("value", "")), started.get(name)
            if start is not None and not start.get("confined"):
                spec = config.AgentSpec(str(event["agent"]), str(start.get("model", "")), str(start.get("effort", "")))
                found[role] = Asked(spec, role, value)
            elif start is None and role in found and found[role].session != value:
                found.pop(role)  # a resumed step whose session is not the one its agent started with
    for role in ASKED:
        if role in found and step.find_transcript(
            {"agent": found[role].spec.agent, "kind": "id", "value": found[role].session}, Path.home()
        ):
            return found[role]
    spec = next((s for s in fallback.entries(ctx.settings, "fix") if s.agent in FORK), None)
    return Asked(spec) if spec else None


def ask_command(ctx: Context, rnd: Round, asked: Asked) -> list[str]:
    spec = asked.spec
    if asked.session:
        fork = [part.format(id=asked.session) for part in FORK[spec.agent]]
        prompt = ASK_COPY.format(number=ctx.pr.number)
    else:
        fork, prompt = [], ASK_NEW.format(number=ctx.pr.number, folder=rnd.folder)
    return [spec.agent, *fork, *spec.args(), *step.NO_MEMORY[spec.agent], *step.NO_UPDATE_PROMPT[spec.agent],
            *READ_ONLY[spec.agent], prompt]


def ask(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    """Open the agent ASKED says in a new tab of the run's workspace and take the user
    there; the menu stays in its own tab."""

    asked = to_ask(ctx, rnd)
    if asked is None:
        raise PRStop("the fix role has no claude or codex agent to ask")
    workspace = str((runlog.last_event(ctx.run.path, "worktree.created") or {}).get("workspace", ""))
    label = f"ask-{asked.role}" if asked.role else "ask"
    try:
        pane = ctx.herdr.create_tab(workspace, ctx.worktree, label, ctx.env)
        ctx.herdr.run_in_pane(pane, shlex.join(ask_command(ctx, rnd, asked)))
    except HerdrError as error:
        raise PRStop(f"could not open the agent's tab: {error}") from error
    ctx.run.event("pr.asked", round=rnd.number, role=asked.role or None, agent=asked.spec.agent,
                  session=asked.session or None, pane=pane)
    try:
        ctx.herdr.focus_tab(ctx.herdr.tab_of(pane))
    except HerdrError as error:
        ctx.run.event("herdr.warning", error=str(error))
    who = _who(asked)
    print(f"{who[0].upper()}{who[1:]} is in the tab {label} of this workspace, read-only. This menu stays here;"
          " a commit made there is reviewed in a new round before anything goes out.")


def _body(ctx: Context, rnd: Round) -> Path:
    """The comment as posted: the draft, then placido's hidden marker for later rounds."""

    text = (rnd.folder / "comment.md").read_text(encoding="utf-8").rstrip()
    path = rnd.folder / "posted-comment.md"
    path.write_text(f"{text}\n\n{prgh.marker(rnd.number, rnd.head)}\n", encoding="utf-8")
    return path


def post(ctx: Context, rnd: Round, s: State, confirm: Ask) -> None:
    """Post the comment, shown first as it goes out: a fix may have changed it since the
    summary showed it (decided 2026-10-16, after #592's comment went out unseen)."""

    kind, how = POSTED_AS[s.verdict]
    unpushed = " It may say what was changed on the branch, which is not pushed yet." if s.ahead else ""
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
    """Push the head the round reviewed in full, with the user's own commits on it, to
    the contributor's branch, never forced."""

    if not s.pull.can_modify:
        raise PRStop("the contributor does not let maintainers push to the branch")
    why = blocked(rnd, s)
    if why:
        raise PRStop(f"nothing is pushed: {why}")
    target = f"{s.pull.head_repo}:{s.pull.head_ref}"
    left = f" Round {rnd.number} leaves {len(s.kept)} minor finding(s) unfixed." if s.kept else ""
    if not confirm(f"Push {s.ahead} commit(s) to {target}, reviewed in full in round {rnd.number}{_trusted(s)}?"
                   f" (never forced){left}"):
        return
    url = PUSH_URL.format(repo=s.pull.head_repo)
    done = subprocess.run(  # the head checked above, whatever the worktree moved to meanwhile
        ["git", "-C", str(ctx.worktree), "push", url, f"{s.outgoing}:refs/heads/{s.pull.head_ref}"],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise PRStop(f"git push: {(done.stderr or done.stdout).strip()[-400:]}")
    commit = s.outgoing
    ctx.run.event("pr.pushed", round=rnd.number, commit=commit, to=target)
    print(f"pushed {commit[:12]} to {target}")
    if not _shown(ctx, commit):
        print(f"GitHub does not show #{s.pull.number} at {commit[:12]} yet: until it does, the menu counts"
              " the commits as not pushed")


def _shown(ctx: Context, commit: str) -> bool:
    """Whether GitHub shows the pull request at the commit pushed within PUSH_SHOWN
    seconds: as its head, and as the ref the commits not pushed are counted against."""

    end = time.monotonic() + PUSH_SHOWN
    while True:
        pull = ctx.hub.pull_request(ctx.pr.number)
        pr.fetch(ctx.root, pull.base, pull.number)
        if pull.head_sha == commit and pr.git(ctx.root, "rev-parse", pr.pr_ref(pull.number)) == commit:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(2)


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
    """Squash-merge the pull request, pinned to the head the round reviewed in full, with
    the user's own commits on it."""

    why = blocked(rnd, s)
    if why:
        raise PRStop(f"nothing is merged: {why}")
    head = s.pull.head_sha
    if s.ahead:
        raise PRStop(f"the pull request lacks {s.ahead} commit(s) made here: push them first")
    if head != s.outgoing:
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
    # Without the judge's draft (a review from before 2026-10-17 whose changes were asked
    # of the contributor), GitHub's own.
    text = drafted.read_text(encoding="utf-8") if drafted.is_file() else f"{s.pull.title} (#{number})"
    lines = text.strip().splitlines()
    subject = lines[0].strip()
    body = rnd.folder / "merge-body.md"
    body.write_text("\n".join(lines[1:]).strip() + "\n", encoding="utf-8")
    # Pending CI is waited out here, and the merge follows its green without asking again,
    # so the user can leave it (decided 2026-10-16).
    later = f" once CI is green on {head[:12]}? Placido waits up to {wait} and merges then" if ci == "pending" else "?"
    left = f" Round {rnd.number} leaves {len(s.kept)} minor finding(s) unfixed." if s.kept else ""
    if not confirm(f"Squash-merge #{number} into {s.pull.base} as \"{subject}\"{later}{left}"):
        return
    if ci == "pending":
        ctx.run.event("pr.merge_waiting", round=rnd.number, head=head)
        status.show(ctx.run.path, status.text(status.WAITING, f"CI on #{number}, then the merge"))
        ci, failed = github._await_ci(ctx.hub, number, head, ctx.settings.ci_wait, time.sleep, time.monotonic,
                                      github._progress(ctx.run, f"CI on #{number}"), arrived=True)
        if ci != "green":
            why = {"red": f"CI went red on {head[:12]} ({', '.join(c.get('name', '?') for c in failed)})",
                   "pending": f"CI was still running on {head[:12]} after {wait}",
                   "moved": f"the pull request moved on from {head[:12]} while CI ran",
                   }.get(ci, f"CI showed no result on {head[:12]}")
            pr.notify(ctx, "not merged", f"#{number}: {why}")
            then = "" if ci == "moved" else f" See {s.pull.url}/checks"  # the menu shows what the new head allows
            raise PRStop(f"{why}; nothing was merged.{then}")
    ctx.hub.merge(number, head, subject, body)
    ctx.run.event("pr.merged", round=rnd.number, head=head, subject=subject)
    status.show(ctx.run.path, status.text(status.DONE, f"merged; placido close pr-{number}"))
    if later != "?":
        pr.notify(ctx, "merged", f"#{number} into {s.pull.base} once CI was green; placido close pr-{number}",
                  mail=False)
    print(f"merged #{number}")  # the menu then says how to end the run


# ---- fixing it here ---------------------------------------------------------------------------


def fix(ctx: Context, rnd: Round, s: State, confirm: Ask) -> Round:
    """Fix what the round kept, here, on the contributor's branch (the base merged in
    first when it moved on), then review the branch with the fix in full, in a new
    round; while that round keeps findings, fix them and review again, up to `[pr]
    fix_rounds` times. Placido runs the gates and commits; nothing is pushed. Returns
    the round reviewed last, which the menu goes on with."""

    if s.fixed:
        raise PRStop(f"round {rnd.number}'s review was answered here already: review that in full first")
    rounds = ctx.settings.pr.fix_rounds
    for n in range(1, rounds + 1):
        data = final(rnd)
        items = [f for f in data["findings"] if f["decision"] == "keep"]
        if prcheck.verdict_of(data) != "fix-here" or not items:
            break
        fix_round(ctx, rnd, items)
        print(f"\nround {rnd.number + 1} reviews the branch with the fix in full")
        rnd = pragents.review(ctx, quiet=True)
        data = final(rnd)
        kept = [f["id"] for f in data["findings"] if f["decision"] == "keep"]
        print(f"round {rnd.number}: {prcheck.verdict_of(data)}" + (f", keeping {', '.join(kept)}" if kept else ""))
    verdict = prcheck.verdict_of(final(rnd))
    more = (f"; still fix-here after {rounds} fix(es): placido pr {ctx.pr.number} fixes again"
            if verdict == "fix-here" else f"; placido pr {ctx.pr.number}")
    pr.notify(ctx, "the fixes are reviewed", f"#{ctx.pr.number}: round {rnd.number}, {verdict}{more}", mail=False)
    return rnd


def review_here(ctx: Context, rnd: Round, s: State, confirm: Ask) -> Round:
    """Review in full, in a new round, a fix made here that no round has read yet, with
    the user's own commits since."""

    reviewed = pragents.review(ctx)
    if reviewed.number == rnd.number:
        raise PRStop(f"nothing here is new since round {rnd.number}'s review")
    return reviewed


def fix_round(ctx: Context, rnd: Round, items: list[dict[str, Any]]) -> None:
    """Merge the base in when it moved on, then one fix of the items: the fix role
    answers each, placido runs the gates and commits."""

    attempt_no = len(pr.round_events(ctx.run.path, "pr.fixed", rnd.number)) + 1
    ctx.run.event("pr.fix_start", round=rnd.number, findings=[f["id"] for f in items])
    merge_base_in(ctx, rnd)
    # The stack, unless it is up as the round set it: a merge that changed host paths,
    # such as a Dockerfile main has since changed (#626), leaves it to be set up again
    # from them, and a setup that failed is tried again when the fix is picked again (#630).
    pr.setup(ctx, rnd)
    fix_once(ctx, rnd, items, attempt_no)


# ---- the base merged in before a fix ---------------------------------------------------

# A line git writes around a conflict; `=======` alone is left out, being a Markdown
# heading's underline too.
MARKER = re.compile(r"^(<{7}|>{7}|\|{7})( |$)", re.MULTILINE)


def merge_base_in(ctx: Context, rnd: Round) -> None:
    """Merge the base branch into the branch, as a commit of its own, when it has moved
    past it and the pull request changes no host path. A conflict goes to the fix role,
    which resolves the conflicts and nothing else; the gates run with the fix after.
    A merge that changes host paths sets the stack up again, from what it merged."""

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
    paths = ctx.settings.pr.host_paths
    host = pr.git(wt, "diff", "--name-only", head, commit, "--", *paths).splitlines() if paths else []
    run.event("pr.base_merged", round=rnd.number, commit=commit, tip=tip, conflicts=conflicts, host=host)
    print(f"merged {base} ({tip[:12]}) into the branch as {commit[:12]}"
          + (f", resolving {', '.join(conflicts)}" if conflicts else "") + "; not pushed")
    if host:
        print(f"{base} changed {', '.join(host)}, so placido sets the stack up again")


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
            pr.approve_host_change(ctx, rnd, f"fix {attempt_no}")  # before the gates below run any of it
        if result.read_text(encoding="utf-8").strip().splitlines()[:1] == [NO_CHANGES]:
            return [f"the result says '{NO_CHANGES}', but the worktree has changes."] if pr.git(wt, "status", "--porcelain") else []
        return implement.check(result, wt, head, entry, settings, ctx.env, run)

    name = f"r{rnd.number}-fix-{attempt_no}"

    def start(spec: config.AgentSpec) -> Any:
        folder = run.new_step_dir(name)
        return ctx.runner(
            "fix", spec, fix_prompt(ctx, rnd, listed, folder), name=name, folder=folder, check=check,
            ask=implement.blocked,
        )

    step = pragents.attempt(ctx, chain, start)
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
        approved = pr.untrusted_host_files(ctx, rnd, pr.primary_lane(ctx))
        commit = _commit(wt, implement.message(result), run, rnd)
        if approved:
            run.event("pr.host_change_committed", round=rnd.number, commit=commit, files=approved)
    run.event("pr.fixed", round=rnd.number, attempt=attempt_no, commit=commit, step=step.path.name, **counts)
    pr.settle(ctx, rnd, f"fix {attempt_no}")
    print(f"\nfix {attempt_no}: {counts['fixed']} fixed, {counts['disputed']} disputed"
          + (f"; committed as {commit[:12]}, not pushed" if commit else "; no code changed"))
    return step.path


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


def fix_prompt(ctx: Context, rnd: Round, listed: Path, folder: Path) -> str:
    """The fixer's task: every finding the round kept."""

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
        f"- **The findings to fix, all of them:** {listed}",
    ]
    merged = pr.done(ctx.run.path, "pr.base_merged", rnd.number)
    if merged:
        lines.append(
            f"- **The base:** placido merged {ctx.pr.base} ({merged['tip'][:12]}) into the branch before you, as"
            f" {merged['commit'][:12]}, so you fix what will land (`git -C {ctx.worktree} show --remerge-diff"
            f" {merged['commit'][:12]}` shows how its conflicts were resolved). A finding that asks for a rebase"
            " or a merge is answered by it: fix what remains, such as tests the base's changes broke."
        )
    skipped = pr.done(ctx.run.path, "pr.base_merge_skipped", rnd.number)
    if skipped and not merged:
        tip = skipped["tip"]
        # #627: a finding asked for the merge, and a fixer that made it was refused, over and over.
        clash = subprocess.run(
            ["git", "-C", str(ctx.worktree), "merge-tree", "--write-tree", "--name-only", "--no-messages", tip, "HEAD"],
            capture_output=True, text=True,
        )
        files = clash.stdout.splitlines()[1:] if clash.returncode == 1 else []
        lines.append(
            f"- **The base:** {ctx.pr.base} has moved on to {tip[:12]}, but the branch changes host paths, so"
            " placido did not merge it in, and you must not either, whatever a finding asks. "
            + (f"These files conflict with {ctx.pr.base}: {', '.join(f for f in files if f)}. Where"
               f" {ctx.pr.base} already makes the branch's change to one, put it back as the branch's base"
               f" ({rnd.base[:12]}) has it (`git checkout {rnd.base[:12]} -- <file>`), so {ctx.pr.base}'s copy"
               " lands whole; dispute a conflict that cannot be settled that way."
               if files else f"Nothing in the branch conflicts with {ctx.pr.base}.")
        )
    gates = ", ".join(ctx.settings.pr.gates) or "none"
    host = ", ".join(ctx.settings.pr.host_paths) or "none"
    lines += [
        f"- **Paths that run on this machine:** {host}. Change them only when a finding needs it:"
        " the round after yours reviews the change in full, as it does the rest.",
        f"- **Gates:** {gates}. Placido runs them after you and commits only when they pass, so"
        " run only the narrow tests for what you change. Then a new round reviews the pull request"
        " with your fixes in full, as it reviewed the contributor's head.",
        decisions.prompt_line(ctx.run.path, pragents.OVER),
        f"- **Write:** {folder / 'resolutions.json'} and the result file.",
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
    "fix": fix,
    "review": review_here,
    "last": last,
    "push": push,
    "post": post,
    "ci": approve_ci,
    "merge": merge,
    "ask": ask,
}
