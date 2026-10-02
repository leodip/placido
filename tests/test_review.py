import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from placido import config, decisions, review, runlog
from placido.outcomes import Failure
from placido.step import StepResult, next_step_number


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def finding(n: int, severity: str, round_: int = 1, security: bool = False, axis: str = "spec") -> dict:
    return {
        "id": f"R{round_}-{n}", "axis": axis, "severity": severity, "security": security,
        "location": "greet.py:3", "title": f"problem {n}", "description": "what is wrong", "fix": "how",
    }


class FakeRunner:
    """Plays the reviewer and the fixer from a script of rounds."""

    def __init__(self, run: runlog.Run, worktree: Path) -> None:
        self.run, self.worktree = run, worktree
        self.reviews: list[dict] = []  # per round: {"findings": [...], "verdicts": [...]}
        self.fixes: list[dict] = []  # per fix: {"answers": {id: status}, "change": "code" | "test" | None}
        self.calls: list[tuple] = []
        self.closed: list[str] = []
        self.fail_review = False
        self.failures: list[str | None] = []  # per step started or resumed: a failed turn's kind, or None
        self.models: list[tuple[str, str]] = []  # (role, model) of each fresh step
        self.tasks: list[str] = []

    def _folder(self, name: str) -> Path:
        return self.run.step_dir(next_step_number(self.run), name)

    def __call__(self, role, agent, task, name=None, check=None, keep_open=False, **kwargs):
        self.calls.append(("start", role, name, keep_open))
        self.models.append((role, agent.model))
        self.tasks.append(task)
        folder = self._folder(name)
        failed = self._failed(folder, role)
        if failed:
            return failed
        if role == "review":
            return self._review(folder, check)
        return self._fix(folder, check)

    def resume(self, role, kind, agent, pane, task, name=None, check=None, keep_open=False):
        self.calls.append(("resume", role, name, agent))
        self.tasks.append(task)
        folder = self._folder(name)
        return self._failed(folder, role) or self._review(folder, check)

    def _failed(self, folder: Path, role: str) -> StepResult | None:
        kind = self.failures.pop(0) if self.failures else None
        if kind is None:
            return None
        if role == "fix":
            (self.worktree / "greet.py").write_text("half a fix\n")
        agent = ("review-w1-p2", "w1:p2") if role == "review" else ("", "")
        return StepResult(kind, folder, *agent, Failure(kind, f"a {kind} message"))

    def close_agent(self, agent, pane, folder, kind):
        self.closed.append(agent)

    def _review(self, folder: Path, check) -> StepResult:
        if self.fail_review:
            return StepResult("no-result", folder)
        data = self.reviews.pop(0)
        (folder / "findings.json").write_text(json.dumps({"round": 0, "verdicts": [], **data}))
        result = folder / "result.md"
        result.write_text("counts\n")
        problems = check(result)
        assert not problems, problems
        return StepResult("success", folder, "review-w1-p2", "w1:p2")

    def _fix(self, folder: Path, check) -> StepResult:
        plan = self.fixes.pop(0)
        answers = [{"id": i, "status": s, "note": "because"} for i, s in plan["answers"].items()]
        (folder / "resolutions.json").write_text(json.dumps({"resolutions": answers}))
        result = folder / "result.md"
        change = plan.get("change")
        if change:
            target = "greet.py" if change == "code" else "test_greet.py"
            path = self.worktree / target
            path.write_text(path.read_text() + f"# fix {len(self.calls)}\n")
            result.write_text("Fix review findings\n\nWhat changed.\n\n## Evidence\n- ok\n")
        else:
            result.write_text(f"{review.NO_CHANGES}\n")
        problems = check(result)
        return StepResult("invalid" if problems else "success", folder)


class LoopTestCase(unittest.TestCase):
    ROUNDS = 3

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "user.email", "t@t")
        (self.repo / "greet.py").write_text("x = 1\n")
        (self.repo / "test_greet.py").write_text("y = 1\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        cfg = self.tmp / "config.toml"
        cfg.write_text(f"[review]\nfinal_rounds = {self.ROUNDS}\n")
        self.settings = config.load(cfg)
        self.run = runlog.Run.create(self.tmp / "runs", "repo", "03-x", {"base_commit": "abc"})
        self.runner = FakeRunner(self.run, self.repo)
        agent = config.AgentSpec("codex", "gpt-6.1-sol", "max", "fast")
        fixer = config.AgentSpec("claude", "opus", "high")
        self.loop = review.Loop(self.run, self.runner, None, self.settings, agent, fixer, self.repo, {})

    def decisions(self) -> list[tuple]:
        return [
            (e["round"], e["decision"], e["reason"]) for e in runlog.read_events(self.run.path)
            if e["event"] == "review.decision"
        ]


class LoopTest(LoopTestCase):
    def test_a_clean_review_passes_in_one_round(self):
        self.runner.reviews = [{"findings": []}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(self.decisions(), [(1, "pass", "nothing open")])
        self.assertEqual(self.runner.closed, ["review-w1-p2"])
        self.assertEqual([c[1] for c in self.runner.calls], ["review"])

    def test_a_blocking_fix_is_verified_by_the_resumed_reviewer(self):
        self.runner.reviews = [
            {"findings": [finding(1, "blocking")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "resolved"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(self.runner.calls[2], ("resume", "review", "review-2", "review-w1-p2"))
        self.assertEqual(self.runner.calls[0][3], True)  # the reviewer is kept open
        body = sh(self.repo, "git", "log", "-1", "--format=%B")
        self.assertIn("Placido-Review-Round: 1", body)
        self.assertEqual(
            self.decisions(),
            [(1, "next round", "serious findings with a fix or a dispute to judge"),
             (2, "pass", "nothing open")],
        )

    def test_minor_findings_are_fixed_without_another_round(self):
        self.runner.reviews = [{"findings": [finding(1, "minor"), finding(2, "minor")]}]
        self.runner.fixes = [{"answers": {"R1-1": "fixed", "R1-2": "deferred"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(self.decisions(), [(1, "stop", "only minor findings")])
        self.assertEqual(account.unverified, ["R1-1"])
        self.assertEqual([f["id"] for f in account.followups], ["R1-2"])

    def test_a_fix_in_tests_only_is_verified_too(self):
        # The change under review may be test code itself (Goiabada #463).
        self.runner.reviews = [
            {"findings": [finding(1, "significant")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "resolved"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "test"}]
        account = self.loop()
        self.assertEqual(self.decisions()[0], (1, "next round", "serious findings with a fix or a dispute to judge"))
        self.assertEqual(account.unverified, [])

    def test_a_fix_that_changed_nothing_earns_no_round(self):
        self.runner.reviews = [{"findings": [finding(1, "significant")]}]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": None}]
        account = self.loop()
        self.assertEqual(self.decisions(), [(1, "stop", "the fix changed nothing")])
        self.assertEqual(account.unverified, ["R1-1"])

    def test_a_disputed_blocking_finding_is_judged_and_escalated_when_rejected(self):
        self.runner.reviews = [
            {"findings": [finding(1, "blocking")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "rejected-dispute"}]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "rejected-dispute"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "disputed"}}] * 3
        account = self.loop()
        self.assertEqual(account.outcome, "escalated")
        self.assertEqual([f["id"] for f in account.escalated], ["R1-1"])
        self.assertIn("dispute not judged", account.escalated[0]["why"])
        self.assertEqual(self.decisions()[-1], (3, "stop", "round budget used"))

    def test_an_accepted_dispute_passes(self):
        self.runner.reviews = [
            {"findings": [finding(1, "blocking")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "accepted-dispute"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "disputed"}}]
        self.assertEqual(self.loop().outcome, "passed")

    def test_an_escalation_by_the_fixer_reaches_the_user(self):
        self.runner.reviews = [
            {"findings": [finding(1, "significant", security=True)]},
            {"findings": [], "verdicts": []},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "escalated"}}]
        account = self.loop()
        self.assertEqual(account.outcome, "escalated")
        self.assertEqual(account.escalated[0]["why"], "because")

    def test_a_failed_review_step(self):
        self.runner.fail_review = True
        self.assertEqual(self.loop().outcome, "failed")

    def test_summary(self):
        self.runner.reviews = [{"findings": [finding(1, "minor")]}]
        self.runner.fixes = [{"answers": {"R1-1": "deferred"}}]
        account = self.loop()
        text = review.summary(account, self.run.path)
        self.assertIn("# Review: passed", text)
        self.assertIn("- Round 1: 1 new finding (1 minor); no code changed.", text)
        self.assertIn("- **R1-1** minor · spec · `greet.py:3` · problem 1\n  deferred → deferral not judged", text)
        self.assertIn("## Follow-ups", text)
        self.assertTrue((self.run.path / "review.md").is_file())


    def test_summary_tells_what_became_of_each_finding(self):
        self.runner.reviews = [
            {"findings": [finding(1, "blocking"), finding(2, "significant", security=True)]},
            {"findings": [finding(1, "minor", round_=2)],
             "verdicts": [{"id": "R1-1", "verdict": "resolved"}, {"id": "R1-2", "verdict": "unresolved"}]},
            {"findings": [], "verdicts": [{"id": "R1-2", "verdict": "resolved"},
                                          {"id": "R2-1", "verdict": "accepted-dispute"}]},
        ]
        self.runner.fixes = [
            {"answers": {"R1-1": "fixed", "R1-2": "fixed"}, "change": "code"},
            {"answers": {"R1-2": "fixed", "R2-1": "disputed"}, "change": "code"},
        ]
        account = self.loop()
        text = review.summary(account, self.run.path)
        first, second = [e["commit"][:7] for e in account.rounds[:2]]
        self.assertIn(f"- Round 1: 2 new findings (1 blocking, 1 significant); fixes committed as {first}.", text)
        self.assertIn(f"- Round 2: judged 2 earlier findings; 1 new finding (1 minor); fixes committed as {second}.", text)
        self.assertIn("- Round 3: judged 2 earlier findings; no new findings.", text)
        self.assertIn(f"- **R1-1** blocking · spec · `greet.py:3` · problem 1\n  fixed in {first} → resolved (round 2)", text)
        self.assertIn(
            f"- **R1-2** significant · spec · security · `greet.py:3` · problem 2\n"
            f"  fixed in {first} → not resolved (round 2) → fixed in {second} → resolved (round 3)", text,
        )
        self.assertIn("- **R2-1** minor · spec · `greet.py:3` · problem 1\n  disputed → dispute accepted (round 3)", text)

    def test_summary_marks_a_fix_nobody_verified(self):
        self.runner.reviews = [{"findings": [finding(1, "minor")]}]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "code"}]
        text = review.summary(self.loop(), self.run.path)
        self.assertRegex(text, r"\*\*R1-1\*\* .*\n  fixed in [0-9a-f]{7} → not verified")


class BudgetTest(LoopTestCase):
    ROUNDS = 1

    def test_fixes_in_the_last_round_are_reported_unverified(self):
        self.runner.reviews = [{"findings": [finding(1, "blocking")]}]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(account.unverified, ["R1-1"])
        self.assertEqual(self.decisions(), [(1, "stop", "round budget used")])


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def findings(self, data: dict, round_: int = 1, earlier=()) -> list[str]:
        path = self.dir / "findings.json"
        path.write_text(json.dumps(data))
        return review.check_findings(path, round_, list(earlier))

    def resolutions(self, answers: list[dict], findings: list[dict]) -> list[str]:
        path = self.dir / "resolutions.json"
        path.write_text(json.dumps({"resolutions": answers}))
        return review.check_resolutions(path, findings)

    def test_good_findings(self):
        self.assertEqual(self.findings({"findings": [finding(1, "blocking"), finding(2, "minor")]}), [])
        self.assertEqual(self.findings({"findings": []}), [])

    def test_bad_findings(self):
        bad = {**finding(1, "urgent"), "id": "R1-7", "axis": "style"}
        problems = self.findings({"findings": [bad]})
        self.assertIn("finding 1 has id 'R1-7'; it should be R1-1.", problems)
        self.assertIn("finding 1 has axis 'style'; use spec or standards.", problems)
        self.assertIn("finding 1 has severity 'urgent'; use blocking, significant, or minor.", problems)

    def test_later_rounds_need_a_verdict_for_each_earlier_finding(self):
        problems = self.findings({"findings": [], "verdicts": []}, round_=2, earlier=["R1-1"])
        self.assertEqual(problems, ["earlier finding R1-1 has no verdict."])
        ok = {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "resolved"}]}
        self.assertEqual(self.findings(ok, round_=2, earlier=["R1-1"]), [])

    def test_every_finding_needs_an_answer(self):
        problems = self.resolutions([], [finding(1, "minor")])
        self.assertEqual(problems, ["finding R1-1 has no resolution."])

    def test_blocking_cannot_be_deferred(self):
        problems = self.resolutions([{"id": "R1-1", "status": "deferred", "note": "later"}], [finding(1, "blocking")])
        self.assertEqual(problems, ["finding R1-1 is blocking, so it cannot be deferred."])

    def test_serious_security_cannot_be_disputed_or_deferred(self):
        for status in ("disputed", "deferred"):
            with self.subTest(status=status):
                problems = self.resolutions(
                    [{"id": "R1-1", "status": status, "note": "x"}], [finding(1, "significant", security=True)]
                )
                self.assertIn("a significant security finding: fix it or escalate it.", problems[0])
        minor = self.resolutions([{"id": "R1-1", "status": "disputed", "note": "x"}], [finding(1, "minor", security=True)])
        self.assertEqual(minor, [])

    def test_answers_need_a_note(self):
        problems = self.resolutions([{"id": "R1-1", "status": "fixed", "note": " "}], [finding(1, "minor")])
        self.assertEqual(problems, ["finding R1-1 needs a note explaining its fixed answer."])


class FallbackTest(LoopTestCase):
    """The review chain by default: codex gpt-6.1-sol, then codex daybreak-blue, then pi."""

    def test_a_refused_first_round_is_done_by_the_next_agent(self):
        self.runner.failures = ["refusal"]
        self.runner.reviews = [{"findings": []}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(self.runner.models, [("review", "gpt-6.1-sol"), ("review", "gpt-daybreak-blue-latest")])
        self.assertIn("review-w1-p2", self.runner.closed)  # the refused reviewer's tab is closed
        moved = [e for e in runlog.read_events(self.run.path) if e["event"] == "agent.fallback"]
        self.assertEqual([(e["role"], e["reason"]) for e in moved], [("review", "refusal")])

    def test_a_refused_later_round_hands_over_to_a_fresh_reviewer(self):
        self.runner.failures = [None, None, "refusal"]  # review 1, fix 1, then the resumed review 2
        self.runner.reviews = [
            {"findings": [finding(1, "blocking")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "resolved"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(self.runner.calls[2][0], "resume")
        self.assertEqual(self.runner.calls[3][:3], ("start", "review", "review-2"))
        self.assertEqual(self.runner.models[-1], ("review", "gpt-daybreak-blue-latest"))
        handover = self.runner.tasks[-1]
        self.assertIn("You take over from an earlier reviewer", handover)
        self.assertIn("01-review-1", handover)
        self.assertIn("Earlier findings to give a verdict on:** R1-1", handover)

    def test_the_reviewer_stays_on_the_fallback_for_the_rest_of_the_review(self):
        self.runner.failures = ["refusal"]
        self.runner.reviews = [
            {"findings": [finding(1, "blocking")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "resolved"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "code"}]
        self.loop()
        self.assertEqual(self.runner.calls[-1][0], "resume")  # round 2 resumes the fallback reviewer

    def test_a_quota_skips_the_same_subscription(self):
        self.runner.failures = ["quota"]
        self.runner.reviews = [{"findings": []}]
        self.loop()
        self.assertEqual(self.runner.models[-1], ("review", "deepseek/deepseek-v4.1-flash"))

    def test_every_reviewer_refusing_fails_the_review(self):
        self.runner.failures = ["refusal"] * 3
        account = self.loop()
        self.assertEqual(account.outcome, "failed")
        self.assertEqual(len(self.runner.models), 3)

    def test_a_context_overflow_gets_one_fresh_session(self):
        self.runner.failures = ["context", "context"]
        self.assertEqual(self.loop().outcome, "failed")
        self.assertEqual(self.runner.models, [("review", "gpt-6.1-sol")] * 2)

    def test_a_refused_fixer_is_replaced_and_starts_clean(self):
        self.runner.failures = [None, "refusal"]  # review 1, then the fixer
        self.runner.reviews = [
            {"findings": [finding(1, "blocking")]},
            {"findings": [], "verdicts": [{"id": "R1-1", "verdict": "resolved"}]},
        ]
        self.runner.fixes = [{"answers": {"R1-1": "fixed"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(self.runner.models[1:3], [("fix", "opus"), ("fix", "gpt-daybreak-blue-latest")])
        self.assertIn("review round 1 fix failed (refusal", sh(self.repo, "git", "stash", "list"))
        self.assertNotIn("half a fix", (self.repo / "greet.py").read_text())

    def test_a_logged_out_reviewer_fails_the_review(self):
        self.runner.failures = ["auth"]
        self.assertEqual(self.loop().outcome, "failed")
        self.assertEqual(len(self.runner.models), 1)

    def test_a_new_review_starts_again_on_the_configured_reviewer(self):
        self.runner.failures = ["refusal"]
        self.runner.reviews = [{"findings": []}, {"findings": []}]
        self.loop()
        self.loop.reviewer = config.AgentSpec("codex", "gpt-6.1-sol", "max", "fast")
        self.loop()
        self.assertEqual(self.runner.models[-1], ("review", "gpt-6.1-sol"))


FOLLOWUP = {"id": "F1", "title": "Mention --bye in the usage line", "description": "usage omits it",
            "why": "the agreement froze the usage line"}


class FollowUpTest(LoopTestCase):
    """Out-of-scope work found in round 1 is offered to the user once, at the end."""

    def test_round_1_lists_follow_ups_and_later_rounds_may_not(self):
        path = self.tmp / "findings.json"
        path.write_text(json.dumps({"findings": [], "followups": [FOLLOWUP]}))
        self.assertEqual(review.check_findings(path, 1, []), [])
        self.assertIn("list follow-ups in round 1 only; later rounds verify.", review.check_findings(path, 2, []))
        path.write_text(json.dumps({"findings": [], "followups": [{"id": "X", "title": "t"}]}))
        self.assertEqual(review.check_findings(path, 1, []), ["follow-up 1 needs id F1."])

    def test_unchosen_follow_ups_are_kept_with_why(self):
        offered = []
        self.loop.ask_fold = lambda items: offered.append(items) or []
        self.runner.reviews = [{"findings": [], "followups": [FOLLOWUP]}]
        account = self.loop()
        self.assertEqual(offered, [[FOLLOWUP]])
        self.assertEqual(account.followups, [FOLLOWUP])
        text = review.summary(account, self.run.path)
        self.assertIn("- **F1** (out of scope) Mention --bye in the usage line: the agreement froze", text)

    def test_a_chosen_follow_up_is_decided_built_and_verified(self):
        self.loop.ask_fold = lambda items: [0]
        self.runner.reviews = [
            {"findings": [], "followups": [FOLLOWUP]},
            {"findings": [], "verdicts": [{"id": "F1", "verdict": "resolved"}]},
        ]
        self.runner.fixes = [{"answers": {"F1": "fixed"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "passed")
        self.assertEqual(account.folded, [FOLLOWUP])
        self.assertEqual(account.followups, [])
        self.assertEqual([c[0] for c in self.runner.calls], ["start", "start", "resume"])  # review, fix, verify
        self.assertIn("Fold in: Mention --bye in the usage line?", decisions.path(self.run.path).read_text())
        text = review.summary(account, self.run.path)
        commit = account.rounds[1]["commit"][:7]
        self.assertIn(f"- Round 2: folded in 1 follow-up at your request; fixes committed as {commit}.", text)
        self.assertIn(f"- **F1** significant · spec · Mention --bye in the usage line\n  fixed in {commit} → resolved (round 3)", text)
        self.assertIn("## Folded in at your request\n\n- **F1** Mention --bye in the usage line", text)

    def test_folded_work_left_unresolved_goes_to_the_user(self):
        self.loop.ask_fold = lambda items: [0]
        self.runner.reviews = [
            {"findings": [], "followups": [FOLLOWUP]},
            {"findings": [finding(1, "minor", round_=3)], "verdicts": [{"id": "F1", "verdict": "unresolved", "note": "half"}]},
        ]
        self.runner.fixes = [{"answers": {"F1": "fixed"}, "change": "code"}]
        account = self.loop()
        self.assertEqual(account.outcome, "escalated")
        self.assertEqual(account.escalated[0]["why"], "folded in, but not resolved: half")
        self.assertEqual([f["id"] for f in account.followups], ["R3-1"])

    def test_deferred_findings_are_offered_too(self):
        offered = []
        self.loop.ask_fold = lambda items: offered.append([i["id"] for i in items]) or []
        self.runner.reviews = [{"findings": [finding(1, "minor")], "followups": [FOLLOWUP]}]
        self.runner.fixes = [{"answers": {"R1-1": "deferred"}}]
        self.loop()
        self.assertEqual(offered, [["F1", "R1-1"]])

    def test_the_first_round_prompt_lists_the_slices_results(self):
        self.run.event("slice.committed", slice=1)
        folder = self.run.step_dir(1, "implement-slice-1")
        (folder / "result.md").write_text("Build\n\n## Follow-ups\n- x\n")
        text = review.reviewer_prompt(self.run.path, self.repo, self.tmp / "f", 1, 3, self.settings)
        self.assertIn(f"follow-ups to triage:** the `## Follow-ups` section of {folder / 'result.md'}.", text)
