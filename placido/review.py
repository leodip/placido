"""The review loop: a resumed reviewer, a fresh fixer, and rounds that must be earned.

Round 1 always runs. Another round runs only when the round left blocking or
significant findings, the fixer changed something or disputed one, and the budget
allows; the same reviewer session then verifies the fixes rather than searching
again. Any change counts, tests included: on Goiabada #463 the change under review
was test code, and a rewrite fixing a significant finding went unverified because
it touched only a `_test.go` file (decided 2026-10-02). What the
budget leaves open goes to the user (blocking findings, unresolved disputes) or to
follow-ups (significant and minor ones).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from placido import alerts, config, decisions, fallback, implement, runlog, spec
from placido.herdr import Herdr, HerdrError
from placido.step import Step

SKILLS = Path(__file__).resolve().parent.parent / "skills"
SEVERITIES = ("blocking", "significant", "minor")
AXES = ("spec", "standards")
STATUSES = ("fixed", "disputed", "deferred", "escalated")
VERDICTS = (
    "resolved", "unresolved", "accepted-dispute", "rejected-dispute",
    "accepted-deferral", "rejected-deferral",
)
STILL_OPEN = ("unresolved", "rejected-dispute", "rejected-deferral")
NO_CHANGES = "No code changes"


@dataclass
class Review:
    """The account of a review, kept for the summary."""

    rounds: list[dict[str, Any]] = field(default_factory=list)
    escalated: list[dict[str, Any]] = field(default_factory=list)
    followups: list[dict[str, Any]] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)  # fixed, but no round left to verify
    outcome: str = "passed"


# ---- checks on what the agents write -------------------------------------------------


def _load(path: Path) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, f"{path} is missing."
    except ValueError as error:
        return None, f"{path.name} is not valid JSON: {error}."


def check_findings(path: Path, round_: int, earlier: list[str]) -> list[str]:
    """Problems with a round's findings.json, worded for the reviewer to fix."""

    data, error = _load(path)
    if error:
        return [error]
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return ['findings.json needs a "findings" list (empty when there are none).']
    problems = []
    for n, finding in enumerate(data["findings"], 1):
        where = f"finding {n}"
        if not isinstance(finding, dict):
            problems.append(f"{where} is not an object.")
            continue
        if finding.get("id") != f"R{round_}-{n}":
            problems.append(f"{where} has id {finding.get('id')!r}; it should be R{round_}-{n}.")
        if finding.get("axis") not in AXES:
            problems.append(f"{where} has axis {finding.get('axis')!r}; use spec or standards.")
        if finding.get("severity") not in SEVERITIES:
            problems.append(f"{where} has severity {finding.get('severity')!r}; use blocking, significant, or minor.")
        if not isinstance(finding.get("security", False), bool):
            problems.append(f"{where} needs 'security' as true or false.")
        for key in ("title", "description"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                problems.append(f"{where} needs a non-empty {key!r}.")
    verdicts = data.get("verdicts", [])
    if not isinstance(verdicts, list):
        return problems + ['"verdicts" must be a list.']
    judged = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict) or verdict.get("verdict") not in VERDICTS:
            problems.append(f"verdict {verdict!r} needs a 'verdict' of {', '.join(VERDICTS)}.")
            continue
        judged.add(verdict.get("id"))
    for finding_id in earlier:
        if finding_id not in judged:
            problems.append(f"earlier finding {finding_id} has no verdict.")
    followups = data.get("followups", [])
    if not isinstance(followups, list):
        return problems + ['"followups" must be a list.']
    if followups and round_ != 1:
        problems.append("list follow-ups in round 1 only; later rounds verify.")
    for n, item in enumerate(followups, 1):
        if not isinstance(item, dict) or item.get("id") != f"F{n}":
            problems.append(f"follow-up {n} needs id F{n}.")
            continue
        for key in ("title", "description", "why"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                problems.append(f"follow-up F{n} needs a non-empty {key!r}.")
    return problems


def check_resolutions(path: Path, open_findings: list[dict[str, Any]]) -> list[str]:
    """Problems with resolutions.json: every finding answered, within the rules."""

    data, error = _load(path)
    if error:
        return [error]
    entries = data.get("resolutions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return ['resolutions.json needs a "resolutions" list.']
    by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}
    problems = []
    for finding in open_findings:
        answer = by_id.get(finding["id"])
        if answer is None:
            problems.append(f"finding {finding['id']} has no resolution.")
            continue
        status = answer.get("status")
        if status not in STATUSES:
            problems.append(f"finding {finding['id']} has status {status!r}; use {', '.join(STATUSES)}.")
            continue
        if not isinstance(answer.get("note"), str) or not answer["note"].strip():
            problems.append(f"finding {finding['id']} needs a note explaining its {status} answer.")
        if finding["severity"] == "blocking" and status == "deferred":
            problems.append(f"finding {finding['id']} is blocking, so it cannot be deferred.")
        if finding.get("security") and finding["severity"] != "minor" and status in ("disputed", "deferred"):
            problems.append(
                f"finding {finding['id']} is a {finding['severity']} security finding: fix it or escalate it."
            )
    return problems


# ---- prompts -------------------------------------------------------------------------


def _paths(run_dir: Path, worktree: Path, role: str) -> list[str]:
    record = runlog.read_record(run_dir)
    notes = worktree / ".placido" / "notes" / f"{role}.md"
    return [
        f"- **Agreement:** {spec.issue_dir(run_dir) / 'agreement.md'} (sealed; read it in full)",
        f"- **The change:** commits from {record.get('base_commit', '?')[:12]} to HEAD in {worktree}"
        f" (`git log {record.get('base_commit', '?')[:12]}..HEAD`).",
        f"- **Project notes for this role:** {notes if notes.is_file() else 'none'}",
        decisions.prompt_line(run_dir),
    ]


# Said first in every review prompt: what this work is. OpenAI's safety system refused
# codex's reviews of Goiabada, an authorization server, as possible cybersecurity
# risk; saying plainly that this is the owner's defensive review, proven with tests,
# may make that rarer (2026-10-05).
FRAMING = (
    "This review is for the project's owner, before they merge the change: defensive work on"
    " their own code, run against this issue's own isolated development environment. Show any"
    " problem with a test, not with an attack."
)


def reviewer_prompt(
    run_dir: Path, worktree: Path, folder: Path, round_: int, budget: int, settings: config.Config,
    fix_commits: str = "", resolutions: Path | None = None, earlier: list[str] | None = None,
    handover: list[Path] | None = None,
) -> str:
    """handover lists the earlier rounds' folders when a later round starts in a fresh
    session, because the reviewer that wrote them was refused or overflowed."""

    lines = [f"# Review, round {round_} of at most {budget}", "", FRAMING, ""]
    if round_ > 1 and handover:
        lines += [
            f"Follow the placido review skill in {SKILLS / 'review' / 'SKILL.md'}, as a later round.",
            "You take over from an earlier reviewer, whose session could not go on. Its rounds"
            " are in these folders, each with review.md and findings.json; read them first,"
            " together with the agreement and the change:",
            "",
            *[f"- {folder}" for folder in handover],
            "",
            *_paths(run_dir, worktree, "review"),
            "",
            "Then verify the fixes, as the skill's later-rounds section says; do not search afresh.",
            "",
            f"- **Fix commits:** {fix_commits or 'none: the fixer changed no code'}",
            f"- **The fixer's answers:** {resolutions}",
            f"- **Earlier findings to give a verdict on:** {', '.join(earlier or [])}",
        ]
    elif round_ == 1:
        results = implement.slice_results(run_dir)
        lines += [
            f"Follow the placido review skill in {SKILLS / 'review' / 'SKILL.md'}.",
            "",
            *_paths(run_dir, worktree, "review"),
            f"- **Round budget:** {budget}. Treat this round as your only look: later rounds"
            " only verify fixes, and run only if this one finds something broken.",
            "- **The slices' follow-ups to triage:** the `## Follow-ups` section of "
            + (", ".join(str(r) for r in results) if results else "no slice results (none to triage)")
            + ".",
        ]
    else:
        lines += [
            "Verify the fixes, as the skill's later-rounds section says; do not search afresh.",
            "",
            f"- **Fix commits:** {fix_commits or 'none: the fixer changed no code'}",
            f"- **The fixer's answers:** {resolutions}",
            f"- **Earlier findings to give a verdict on:** {', '.join(earlier or [])}",
        ]
    lines += [
        f"- **Write:** {folder / 'review.md'} and {folder / 'findings.json'} (ids R{round_}-1, R{round_}-2, …).",
    ]
    menu = config.command_menu(settings)
    return "\n".join(lines) + "\n\n" + (menu or "The project defines no commands.\n")


def fixer_prompt(
    run_dir: Path, worktree: Path, folder: Path, open_path: Path, review_md: Path, settings: config.Config,
) -> str:
    gates = ", ".join(settings.slice_gates) or "none"
    lines = [
        "# Fix the review's findings",
        "",
        f"Follow the placido fix skill in {SKILLS / 'fix' / 'SKILL.md'}.",
        "",
        *_paths(run_dir, worktree, "fix"),
        f"- **The findings to answer:** {open_path} (all of them), explained in {review_md}.",
        f"- **Gates:** {gates}. Placido runs them after you, and commits only when they pass.",
        f"- **Write:** {folder / 'resolutions.json'}.",
    ]
    menu = config.command_menu(settings)
    return "\n".join(lines) + "\n\n" + (menu or "The project defines no commands.\n")


# ---- the loop ----------------------------------------------------------------------------


class Loop:
    """Runs the rounds; every decision is logged with its reason."""

    def __init__(
        self, run: runlog.Run, runner: Step, herdr: Herdr, settings: config.Config,
        reviewer: config.AgentSpec, fixer: config.AgentSpec, worktree: Path, env: dict[str, str],
    ) -> None:
        self.run, self.runner, self.herdr, self.settings = run, runner, herdr, settings
        self.reviewer, self.fixer, self.worktree, self.env = reviewer, fixer, worktree, env

    def __call__(self) -> Review:
        account = Review()
        budget = self.settings.final_rounds
        self.run.event("review.start", budget=budget)
        # Each review starts on the configured agents; a move to a fallback holds
        # until the review ends, since the same change would likely be refused again.
        starts = lambda event: event.get("event") == "review.start"  # noqa: E731
        self.reviewers = fallback.Chain(
            self.run, "review", fallback.entries(self.settings, "review", self.reviewer), self._notify, starts,
        )
        self.fixers = fallback.Chain(
            self.run, "fix", fallback.entries(self.settings, "fix", self.fixer), self._notify, starts,
        )
        done: list[Path] = []  # the folders of the rounds reviewed so far
        known: dict[str, dict[str, Any]] = {}  # every finding so far, by id
        pending: list[str] = []  # answered findings the next round must judge
        answers: dict[str, dict[str, Any]] = {}
        agent = pane = ""
        last_folder: Path | None = None
        fix_commits, resolutions = "", None
        outside: list[dict[str, Any]] = []  # follow-ups round 1 found out of scope
        for round_ in range(1, budget + 1):
            self.run.event("review.round", round=round_, budget=budget)
            step = self._review_round(round_, budget, agent, pane, fix_commits, resolutions, pending, done)
            overflowed = False
            while step.outcome != "success" and self._carry_on("review", self.reviewers, step, overflowed):
                # A fresh reviewer takes over the same round, from the earlier rounds' files.
                if step.agent:
                    self.runner.close_agent(step.agent, step.pane, step.path, self.reviewer.agent)
                overflowed = overflowed or step.outcome == "context"
                self.reviewer, agent, pane = self.reviewers.current, "", ""
                step = self._review_round(round_, budget, "", "", fix_commits, resolutions, pending, done)
            if step.outcome != "success":
                account.outcome = "failed"
                self.run.event("review.failed", round=round_, outcome=step.outcome)
                pending = []
                break
            agent, pane, last_folder = step.agent or agent, step.pane or pane, step.path
            done.append(step.path)
            data = json.loads((step.path / "findings.json").read_text(encoding="utf-8"))
            new = data["findings"]
            if round_ == 1:
                outside = list(data.get("followups") or [])
            known.update({f["id"]: f for f in new})
            verdicts = data.get("verdicts", [])
            reopened = [v["id"] for v in verdicts if v["verdict"] in STILL_OPEN]
            for verdict in verdicts:  # a rejected deferral or an escalation was sorted already
                if verdict["verdict"] in STILL_OPEN:
                    account.followups = [f for f in account.followups if f["id"] != verdict["id"]]
            counts = {s: sum(1 for f in new if f["severity"] == s) for s in SEVERITIES}
            self.run.event("review.findings", round=round_, reopened=reopened, **counts)
            account.rounds.append({"round": round_, "findings": new, "verdicts": verdicts})
            open_findings = [known[i] for i in reopened + [f["id"] for f in new] if i in known]
            pending = []
            if not open_findings:
                self.run.event("review.decision", round=round_, decision="pass", reason="nothing open")
                break

            fixed = self._fix_round(round_, open_findings, step.path)
            if fixed is None:
                account.outcome = "failed"
                break
            before, after, resolutions, answers = fixed
            account.rounds[-1]["answers"] = {i: a.get("status") for i, a in answers.items()}
            account.rounds[-1]["commit"] = after if after != before else None
            fix_commits = f"{before[:12]}..{after[:12]}" if after != before else ""
            self._sort_answers(open_findings, answers, account)
            pending = [
                f["id"] for f in open_findings
                if answers.get(f["id"], {}).get("status") in ("fixed", "disputed", "deferred")
            ]
            serious = [f for f in open_findings if f["severity"] != "minor"]
            disputed = [f for f in serious if answers.get(f["id"], {}).get("status") == "disputed"]
            changed = after != before
            if not serious:
                reason = "only minor findings"
            elif not changed and not disputed:
                reason = "the fix changed nothing"
            elif round_ == budget:
                reason = "round budget used"
            else:
                self.run.event("review.decision", round=round_, decision="next round",
                               reason="serious findings with a fix or a dispute to judge")
                continue
            self.run.event("review.decision", round=round_, decision="stop", reason=reason)
            if reason != "round budget used":
                account.unverified += [i for i in pending if answers[i]["status"] == "fixed"]
                pending = []
            break

        # What the budget left unjudged: an undecided dispute on a blocking finding goes to
        # the user, other disputes to follow-ups, and fixes are reported as unverified.
        for finding_id in pending:
            finding, answer = known[finding_id], answers.get(finding_id, {})
            status = answer.get("status")
            if status == "fixed":
                account.unverified.append(finding_id)
            elif status == "disputed" and finding["severity"] == "blocking":
                account.escalated.append({**finding, "why": f"dispute not judged: {answer.get('note', '')}"})
            elif status == "disputed":
                account.followups.append({**finding, "why": f"dispute not judged: {answer.get('note', '')}"})
        # What is left out of the change: follow-ups found out of scope, and findings the
        # fixer deferred or the budget left unjudged. They are drafted as issues on the
        # pull request; the run never stops to ask about them, since it runs unattended
        # (a fold-in question waited all night on 2026-10-02).
        account.followups = outside + account.followups
        if account.outcome != "failed" and account.escalated:
            account.outcome = "escalated"
        if agent and last_folder is not None:
            self.runner.close_agent(agent, pane, last_folder, self.reviewer.agent)
        return account

    def _carry_on(self, role: str, chain: fallback.Chain, step: Any, overflowed: bool) -> bool:
        """Whether a failed step gets another go: on the next agent after a refusal or a
        quota, or once in a fresh session after its context overflowed."""

        message = step.failure.message if getattr(step, "failure", None) else ""
        if step.outcome in fallback.SWITCH:
            return chain.switch(step.outcome, message, step=step.path.name)
        if step.outcome == "context" and not overflowed:
            self.run.event("review.fresh_session", role=role, step=step.path.name, reason="context")
            return True
        if step.outcome == "auth":
            self._notify(f"{chain.current.agent} is logged out",
                         f"{message[:160]} · log in, then run placido review again")
        return False

    def _notify(self, title: str, body: str) -> None:
        if self.herdr is None:
            alerts.email(self.run, title, body)
            return
        alerts.notify(self.run, self.herdr, title, body)

    def _review_round(
        self, round_: int, budget: int, agent: str, pane: str, fix_commits: str,
        resolutions: Path | None, earlier: list[str], done: list[Path],
    ):
        name = f"review-{round_}"
        record_dir = self.run.path

        def check(result: Path) -> list[str]:
            return check_findings(result.parent / "findings.json", round_, earlier if round_ > 1 else [])

        def task(folder: Path) -> str:
            return reviewer_prompt(
                record_dir, self.worktree, folder, round_, budget, self.settings,
                fix_commits, resolutions, earlier, handover=done if not agent else None,
            )

        folder = self._next_folder(name)
        if round_ == 1 or not agent:
            return self.runner(
                "review", self.reviewer, task(folder), name=name, check=check, keep_open=True
            )
        return self.runner.resume(
            "review", self.reviewer.agent, agent, pane, task(folder), name=name, check=check,
            keep_open=True,
        )

    def _fix_round(
        self, round_: int, open_findings: list[dict[str, Any]], review_folder: Path,
    ) -> tuple[str, str, Path, dict[str, dict[str, Any]]] | None:
        name = f"fix-{round_}"
        folder = self._next_folder(name)  # created by the step itself, so its number holds
        open_path = review_folder / "open-findings.json"
        open_path.write_text(json.dumps({"findings": open_findings}, indent=2) + "\n", encoding="utf-8")
        before = implement.git(self.worktree, "rev-parse", "HEAD")
        resolutions = folder / "resolutions.json"

        def check(result: Path) -> list[str]:
            problems = decisions.check(result.parent, self.run.path) or check_resolutions(resolutions, open_findings)
            if problems:
                return problems
            first = result.read_text(encoding="utf-8").strip().splitlines()[:1]
            if first == [NO_CHANGES]:
                if implement.git(self.worktree, "status", "--porcelain"):
                    return [f"the result says '{NO_CHANGES}', but the worktree has changes."]
                return []
            entry = {"id": f"review-{round_}", "title": "review fixes", "gates": []}
            return implement.check(result, self.worktree, before, entry, _no_mutations(self.settings), self.env, self.run)

        prompt = fixer_prompt(
            self.run.path, self.worktree, folder, open_path, review_folder / "review.md", self.settings,
        )
        step = self.runner("fix", self.fixer, prompt, name=name, check=check, ask=implement.blocked)
        overflowed = False
        while step.outcome != "success" and self._carry_on("fix", self.fixers, step, overflowed):
            # The next fixer starts clean: the partial fixes are set aside, as for a slice.
            overflowed = overflowed or step.outcome == "context"
            if implement.git(self.worktree, "status", "--porcelain"):
                message = f"placido: review round {round_} fix failed ({step.outcome}, {self.run.path.name})"
                implement.git(self.worktree, "stash", "push", "--include-untracked", "-m", message)
                self.run.event("worktree.stashed", message=message)
            self.fixer = self.fixers.current
            folder = self._next_folder(name)
            resolutions = folder / "resolutions.json"
            prompt = fixer_prompt(
                self.run.path, self.worktree, folder, open_path, review_folder / "review.md", self.settings,
            )
            step = self.runner("fix", self.fixer, prompt, name=name, check=check, ask=implement.blocked)
        if step.outcome != "success":
            self.run.event("review.failed", round=round_, step=name, outcome=step.outcome)
            return None
        result = step.path / "result.md"
        if result.read_text(encoding="utf-8").strip().splitlines()[:1] != [NO_CHANGES]:
            _commit(self.worktree, result, self.run, round_)
        after = implement.git(self.worktree, "rev-parse", "HEAD")
        data = json.loads(resolutions.read_text(encoding="utf-8"))
        answers = {a["id"]: a for a in data["resolutions"] if isinstance(a, dict)}
        self.run.event(
            "review.fixed", round=round_,
            **{s: sum(1 for a in answers.values() if a.get("status") == s) for s in STATUSES},
        )
        return before, after, resolutions, answers

    def _sort_answers(
        self, open_findings: list[dict[str, Any]], answers: dict[str, dict[str, Any]], account: Review
    ) -> None:
        for finding in open_findings:
            answer = answers.get(finding["id"], {})
            if answer.get("status") == "escalated":
                account.escalated.append({**finding, "why": answer.get("note", "")})
            elif answer.get("status") == "deferred":
                account.followups.append({**finding, "why": answer.get("note", "")})

    def _next_folder(self, name: str) -> Path:
        from placido.step import next_step_number

        return self.run.path / "steps" / f"{next_step_number(self.run):02d}-{name}"


def _no_mutations(settings: config.Config) -> config.Config:
    """Fixes are checked like slices, but without the mutation count."""

    from dataclasses import replace

    return replace(settings, mutations=0)


def _commit(worktree: Path, result: Path, run: runlog.Run, round_: int) -> str:
    text = (
        f"{implement.message(result)}\n\nPlacido-Run: {run.path.parent.name}/{run.path.name}\n"
        f"Placido-Review-Round: {round_}\n"
    )
    implement.git(worktree, "add", "-A")
    done = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-F", "-"], input=text, capture_output=True, text=True
    )
    if done.returncode != 0:
        raise implement.ImplementError(f"git commit: {(done.stderr or done.stdout).strip()}")
    sha = implement.git(worktree, "rev-parse", "HEAD")
    run.event("review.committed", round=round_, commit=sha)
    return sha


VERDICTS = {
    "resolved": "resolved",
    "unresolved": "not resolved",
    "accepted-dispute": "dispute accepted",
    "rejected-dispute": "dispute rejected",
    "accepted-deferral": "deferral accepted",
    "rejected-deferral": "deferral rejected",
}

# What an answer left without a later verdict means at the end of the review.
UNJUDGED = {"fixed": "not verified", "disputed": "dispute not judged", "deferred": "deferral not judged"}


def summary(account: Review, run_dir: Path) -> str:
    """The review's account in markdown, written to the run folder and shown at the end:
    each round in a line, then each finding with what became of it."""

    lines = [f"# Review: {account.outcome}", "", "## Rounds", ""]
    for entry in account.rounds:
        lines.append(f"- {_round_line(entry)}")
    findings = [f for entry in account.rounds for f in entry["findings"]]
    if findings:
        lines += ["", "## Findings", ""]
        for finding in findings:
            where = f" · `{finding['location']}`" if finding.get("location") else ""
            security = " · security" if finding.get("security") else ""
            lines.append(
                f"- **{finding['id']}** {finding['severity']} · {finding['axis']}{security}{where}"
                f" · {finding['title']}"
            )
            lines.append(f"  {_story(finding['id'], account.rounds)}")
    if account.escalated:
        lines += ["", "## Needs your decision", ""]
        lines += [f"- **{f['id']}** ({f['severity']}) {f['title']}: {f['why']}" for f in account.escalated]
    if account.followups:
        lines += ["", "## Follow-ups", ""]
        lines += [f"- **{f['id']}** ({f.get('severity', 'out of scope')}) {f['title']}: {f['why']}"
                  for f in account.followups]
    if account.unverified:
        lines += ["", "## Fixed, not verified by a later round", ""]
        lines += [f"- {finding_id}" for finding_id in account.unverified]
    text = "\n".join(lines) + "\n"
    (run_dir / "review.md").write_text(text, encoding="utf-8")
    return text


def _round_line(entry: dict[str, Any]) -> str:
    parts = []
    if entry["verdicts"]:
        parts.append(f"judged {_count(len(entry['verdicts']), 'earlier finding')}")
    new = entry["findings"]
    if new:
        counts = [
            f"{n} {s}" for s in SEVERITIES
            if (n := sum(1 for f in new if f["severity"] == s))
        ]
        parts.append(f"{_count(len(new), 'new finding')} ({', '.join(counts)})")
    else:
        parts.append("no new findings")
    if entry.get("commit"):
        parts.append(f"fixes committed as {entry['commit'][:7]}")
    elif "answers" in entry:
        parts.append("no code changed")
    return f"Round {entry['round']}: " + "; ".join(parts) + "."


def _story(finding_id: str, rounds: list[dict[str, Any]]) -> str:
    """What became of one finding, round by round: each answer and each verdict."""

    steps: list[str] = []
    last_answer = None
    for entry in rounds:
        verdict = next((v for v in entry["verdicts"] if v["id"] == finding_id), None)
        if verdict is not None:
            steps.append(f"{VERDICTS.get(verdict['verdict'], verdict['verdict'])} (round {entry['round']})")
            last_answer = None
        answer = entry.get("answers", {}).get(finding_id)
        if answer is not None:
            commit = entry.get("commit")
            steps.append(f"fixed in {commit[:7]}" if answer == "fixed" and commit else answer)
            last_answer = answer
    if not steps:
        return "not answered"
    if last_answer in UNJUDGED:
        steps.append(UNJUDGED[last_answer])
    return " → ".join(steps)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
