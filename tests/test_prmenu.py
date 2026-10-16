import json
from unittest import mock

from placido import pr, pragents, prgh, prmenu, runlog
from tests.test_pr import PRTestCase, git_commit, sh


class MenuTestCase(PRTestCase):
    """A reviewed pull request, a fork that holds its branch, and a user whose answers
    are scripted."""

    def setUp(self) -> None:
        super().setUp()
        self.fork = self.tmp / "fork.git"
        sh(self.tmp, "git", "init", "-q", "--bare", str(self.fork))
        sh(self.repo, "git", "push", "-q", str(self.fork), f"{self.head}:refs/heads/greeting")
        patch = mock.patch.object(prmenu, "PUSH_URL", str(self.fork))
        patch.start()
        self.addCleanup(patch.stop)
        self.confirms: list[bool] = []
        self.confirmed: list[str] = []

    def review(self, **final) -> pr.Round:
        if final:
            data = self.runner.default_final()
            data.update(final)
            self.runner.final = data
        return pragents.review(self.ctx)

    def confirm(self, question: str) -> bool:
        self.confirmed.append(question)
        return self.confirms.pop(0) if self.confirms else False

    def act(self, key: str, rnd: pr.Round) -> None:
        prmenu.ACTIONS[key](self.ctx, rnd, prmenu.state(self.ctx, rnd), self.confirm)

    def keys(self, rnd: pr.Round) -> list[str]:
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        return [key for key, _ in listed]

    def fork_head(self) -> str:
        return sh(self.fork, "git", "rev-parse", "refs/heads/greeting")

    def publish(self, commit: str) -> None:
        """GitHub moving the pull request to a commit pushed to its branch."""

        sh(self.repo, "git", "push", "-q", "-f", str(self.origin), f"{commit}:refs/pull/7/head")
        self.hub.pull = self.pull(commit)


class SummaryTest(MenuTestCase):
    def test_says_what_to_decide_and_shows_the_comment(self):
        rnd = self.review()
        text = prmenu.summary(self.ctx, rnd)
        self.assertIn("Verdict: fix-here. It greets, nearly.", text)
        self.assertIn("Fixes: here, by you (about 10% of the pull request; the line is 50%)", text)
        self.assertIn("F1  significant bugs        `greet.py:1` the greeting [A1, B1: all]", text)
        self.assertIn("Found outside this change (for you only, never in the comment):\n  - old bug (x.py) [security]", text)
        self.assertIn("Gates: unit pass · lint pass", text)
        self.assertIn("Reviewers: A claude opus xhigh (1 findings); B codex gpt-6.1-sol max (1 findings)", text)
        self.assertIn("---- the comment to the contributor ----\nThanks for this.", text)


class OptionsTest(MenuTestCase):
    def test_after_a_fix_here_review(self):
        rnd = self.review()
        self.assertEqual(self.keys(rnd), ["edit", "edit-merge", "fix", "post", "close", "quit"])

    def test_merge_is_offered_when_nothing_is_kept(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        self.assertIn("merge", self.keys(rnd))
        self.assertNotIn("fix", self.keys(rnd))

    def test_waiting_ci_runs_can_be_approved(self):
        rnd = self.review()
        self.hub.waiting = [{"databaseId": 5, "conclusion": "action_required"}]
        self.assertIn("ci", self.keys(rnd))

    def test_a_closed_pull_request_offers_nothing(self):
        rnd = self.review()
        self.hub.pull = self.pull(self.head, state="MERGED")
        listed, notes = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        self.assertEqual([k for k, _ in listed], ["quit"])
        self.assertIn("placido close pr-7", notes[0])


class PostTest(MenuTestCase):
    def test_a_request_for_changes_is_posted_as_one_with_placidos_marker(self):
        rnd = self.review(verdict="request-changes", routing={"share": 0.7, "estimate": "x", "route": "contributor"})
        self.act("post", rnd)
        kind, body = self.hub.calls[-1][1:]
        self.assertEqual(kind, "request-changes")
        self.assertTrue(body.startswith("Thanks for this.\n\n1. Fix greet.py."))
        self.assertIn(prgh.marker(1, self.head), body)
        self.assertNotIn("post", self.keys(rnd))

    def test_a_fix_here_comment_before_the_push_asks_first(self):
        rnd = self.review()
        self.act("post", rnd)
        self.assertEqual(self.hub.calls, [])
        self.assertIn("nothing is pushed yet", self.confirmed[0])


class FixAndPushTest(MenuTestCase):
    def test_the_fix_is_committed_here_and_pushed_only_when_confirmed(self):
        rnd = self.review()
        self.act("fix", rnd)
        fix = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.assertNotEqual(fix, self.head)
        message = sh(self.worktree, "git", "log", "-1", "--format=%B")
        self.assertTrue(message.startswith("Greet properly\n\nThe review asked for it."))
        self.assertIn("Placido-PR-Round: 1", message)
        self.assertEqual(runlog.last_event(self.run.path, "pr.fixed")["commit"], fix)
        self.assertEqual(self.keys(rnd), ["edit", "edit-merge", "push", "post", "close", "quit"])

        self.act("push", rnd)  # declined
        self.assertEqual(self.fork_head(), self.head)
        self.confirms = [True]
        self.act("push", rnd)
        self.assertEqual(self.fork_head(), fix)
        self.assertEqual(runlog.last_event(self.run.path, "pr.pushed")["commit"], fix)

    def test_a_push_never_overwrites_what_the_contributor_pushed_meanwhile(self):
        rnd = self.review()
        self.act("fix", rnd)
        sh(self.repo, "git", "checkout", "-q", "-B", "theirs", self.head)
        (self.repo / "theirs.txt").write_text("theirs\n")
        theirs = git_commit(self.repo, "theirs")
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "push", "-q", str(self.fork), f"{theirs}:refs/heads/greeting")
        self.confirms = [True]
        with self.assertRaisesRegex(pr.PRStop, "git push"):
            self.act("push", rnd)
        self.assertEqual(self.fork_head(), theirs)

    def test_no_push_when_maintainers_may_not_modify_the_branch(self):
        rnd = self.review()
        self.act("fix", rnd)
        self.hub.pull = self.pull(self.head, can_modify=False)
        listed, notes = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        self.assertNotIn("push", [k for k, _ in listed])
        self.assertIn("does not let maintainers push", notes[0])

    def test_a_fix_that_touches_a_host_path_is_refused(self):
        rnd = self.review()
        self.runner.during["r1-fix-1"] = lambda wt: (wt / ".placido" / "stack.sh").write_text("evil\n")
        with self.assertRaisesRegex(pr.PRStop, "the fix did not finish"):
            self.act("fix", rnd)
        self.assertIn("which run on this machine; undo those changes", self.runner.problems[-1][1][0])
        self.assertEqual(sh(self.worktree, "git", "status", "--porcelain"), "")  # set aside in a stash
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD"), self.head)


class MergeTest(MenuTestCase):
    def fixed_and_pushed(self) -> tuple[pr.Round, str]:
        rnd = self.review()
        self.act("fix", rnd)
        self.confirms = [True]
        self.act("push", rnd)
        fix = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.publish(fix)
        return rnd, fix

    def test_merges_the_reviewed_head_once_ci_is_green(self):
        rnd, fix = self.fixed_and_pushed()
        self.assertIn("merge", self.keys(rnd))
        self.hub.ci = [{"name": "unit", "bucket": "pass"}]
        self.confirms = [True]
        self.act("merge", rnd)
        self.assertEqual(self.hub.calls[-1], ("merge", fix, "Add a greeting (#7)", "It greets.\n"))
        self.assertIn("pr.merged", self.kinds())

    def test_red_ci_refuses(self):
        rnd, _ = self.fixed_and_pushed()
        self.hub.ci = [{"name": "unit", "bucket": "fail"}]
        with self.assertRaisesRegex(pr.PRStop, "CI is red"):
            self.act("merge", rnd)

    def test_a_head_the_review_did_not_cover_refuses(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        sh(self.repo, "git", "checkout", "-q", "-B", "late", self.head)
        (self.repo / "late.txt").write_text("late\n")
        late = git_commit(self.repo, "late")
        sh(self.repo, "git", "checkout", "-q", "main")
        self.publish(late)
        with self.assertRaisesRegex(pr.PRStop, "which this review did not cover"):
            self.act("merge", rnd)
        self.assertNotIn("merge", [c[0] for c in self.hub.calls])

    def test_no_ci_asks_first(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        self.act("merge", rnd)
        self.assertIn("CI shows no result", self.confirmed[0])
        self.assertEqual(self.hub.calls, [])


class CloseAndCITest(MenuTestCase):
    def test_close_posts_the_comment_then_closes(self):
        rnd = self.review(verdict="close", routing={"share": 1, "estimate": "x", "route": "none"})
        self.confirms = [True]
        self.act("close", rnd)
        self.assertEqual([c[0] for c in self.hub.calls], ["comment", "close"])
        self.assertIn(prgh.marker(1, self.head), self.hub.calls[0][1])

    def test_ci_runs_are_approved_and_workflow_changes_asked_about(self):
        rnd = self.review()
        self.hub.waiting = [{"databaseId": 5}, {"databaseId": 6}]
        self.act("ci", rnd)
        self.assertEqual(self.hub.calls, [("approve", 5), ("approve", 6)])

        head = self.push_new_head({".github/workflows/ci.yml": "on: push\n"}, self.head)
        self.runner.earlier = ["F1"]
        rnd = pragents.review(self.ctx)
        self.hub.calls.clear()
        self.act("ci", rnd)
        self.assertIn(".github/workflows/ci.yml", self.confirmed[-1])
        self.assertEqual(self.hub.calls, [])
        self.assertEqual(rnd.head, head)


class MenuLoopTest(MenuTestCase):
    def test_picked_actions_run_in_a_safe_order_and_the_loop_ends_on_enter(self):
        rnd = self.review(verdict="request-changes", routing={"share": 0.7, "estimate": "x", "route": "contributor"})
        self.hub.waiting = [{"databaseId": 9}]
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        numbers = {key: str(n) for n, (key, _) in enumerate(listed, 1)}
        answers = iter([f"{numbers['ci']} {numbers['post']}", ""])
        prmenu.menu(self.ctx, rnd, lambda prompt: next(answers), self.confirm)
        self.assertEqual([c[0] for c in self.hub.calls], ["review", "approve"])

    def test_merge_and_close_together_are_refused(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        numbers = {key: str(n) for n, (key, _) in enumerate(listed, 1)}
        answers = iter([f"{numbers['merge']},{numbers['close']}", None])
        prmenu.menu(self.ctx, rnd, lambda prompt: next(answers), self.confirm)
        self.assertEqual(self.hub.calls, [])

    def test_resolutions_answer_every_finding(self):
        path = self.tmp / "resolutions.json"
        path.write_text(json.dumps({"resolutions": [{"id": "F1", "status": "skipped", "note": "x"}]}))
        self.assertEqual(prmenu.check_resolutions(path, ["F1", "F2"]), [
            "finding F1 needs a status of fixed, disputed and a note.", "finding F2 has no resolution.",
        ])


class ClosingTest(MenuTestCase):
    def test_host_paths_are_put_back_before_the_teardown_runs_from_them(self):
        from placido import close, config
        from placido.herdr import Herdr

        self.review()
        (self.worktree / ".placido" / "stack.sh").write_text("echo evil\n")
        settings = self.load('[project]\nteardown = "sh .placido/stack.sh > $OUT"\n')
        with mock.patch.dict("os.environ", {"OUT": str(self.tmp / "teardown.out")}):
            close.close(self.run, Herdr(self.fake), settings, force=False)
        self.assertEqual((self.tmp / "teardown.out").read_text(), "stack\n")
        self.assertEqual(runlog.last_event(self.run.path, "close.host_paths_restored")["files"], [".placido/stack.sh"])
        self.assertFalse(self.worktree.exists())
        self.assertIsInstance(config.load(None), config.Config)

    def test_the_branch_goes_once_the_pull_request_is_settled_and_holds_nothing_unpushed(self):
        from placido import close
        from placido.proc import Result

        record = runlog.read_record(self.run.path)
        with mock.patch.object(close.start, "origin_repo", return_value="o/proj"):
            merged = lambda argv, timeout=None: Result(0, "MERGED")  # noqa: E731
            self.assertEqual(close._reviewed(self.repo, "placido/pr-7", record, merged), "pull request #7 merged")
            pr.fetch(self.repo, "main", 7)
            open_ = lambda argv, timeout=None: Result(0, "OPEN")  # noqa: E731
            self.assertIsNone(close._reviewed(self.repo, "placido/pr-7", record, open_))
            (self.worktree / "mine.txt").write_text("unpushed\n")
            git_commit(self.worktree, "mine")
            self.assertIsNone(close._reviewed(self.repo, "placido/pr-7", record, merged))


class FixGatesTest(MenuTestCase):
    def test_a_gate_that_writes_a_host_path_during_the_fix_is_never_committed(self):
        rnd = self.review()
        self.settings = self.load(
            "[commands.unit]\nrun = \"echo evil >> .placido/stack.sh\"\n[gates]\nslice = [\"unit\"]\n"
            "[pr]\ngates = []\n"
        )
        self.ctx = self.context()
        with self.assertRaisesRegex(pr.PRStop, r"\.placido/stack\.sh changed since they were trusted"):
            self.act("fix", rnd)
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD"), self.head)


class VerifyTest(MenuTestCase):
    def failing(self, note: str = "ru still gets the sr copy") -> dict:
        return {
            "verdicts": [{"id": "F1", "verdict": "unresolved", "note": note}],
            "findings": [{
                "id": "V1", "area": "bugs", "severity": "significant", "size": "small", "location": "greet.py:1",
                "title": "the fix broke es-MX", "description": "d", "evidence": "a test", "fix": "f",
            }],
        }

    def test_a_fix_is_checked_by_another_vendor_before_it_is_offered(self):
        rnd = self.review()
        self.act("fix", rnd)
        checked = runlog.last_event(self.run.path, "pr.verified")
        self.assertEqual((checked["passed"], checked["agent"]), (True, "codex"))
        verify = next(c for c in self.runner.calls if c["name"] == "r1-verify-1")
        self.assertEqual(verify["spec"].agent, "codex")
        self.assertIn("resolutions.json", verify["task"])
        self.assertIn(f"log -p {self.head[:12]}..", verify["task"])
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        self.assertIn(("push", "Push the 1 commit(s) made here, checked, to alice/proj:greeting"), listed)
        self.assertNotIn("fix", [k for k, _ in listed])

    def test_what_the_check_leaves_open_goes_back_to_the_fixer(self):
        rnd = self.review()
        self.runner.verify = [self.failing()]
        self.act("fix", rnd)
        self.assertEqual([c["name"] for c in self.runner.calls if "fix" in c["name"] or "verify" in c["name"]],
                         ["r1-fix-1", "r1-verify-1", "r1-fix-2", "r1-verify-2"])
        second = json.loads((rnd.folder / "fix-findings-2.json").read_text())["findings"]
        self.assertEqual([f["id"] for f in second], ["F1", "V1-1"])
        self.assertEqual(second[0]["check"], "unresolved: ru still gets the sr copy")
        verdicts = [e["passed"] for e in runlog.read_events(self.run.path) if e["event"] == "pr.verified"]
        self.assertEqual(verdicts, [False, True])
        self.assertIn("checked", dict(prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))[0])["push"])

    def test_a_minor_new_problem_does_not_send_the_fix_back(self):
        rnd = self.review()
        data = self.failing()
        data["verdicts"][0]["verdict"] = "resolved"
        data["findings"][0]["severity"] = "minor"
        self.runner.verify = [data]
        self.act("fix", rnd)
        self.assertNotIn("r1-fix-2", self.runner.names())
        self.assertTrue(runlog.last_event(self.run.path, "pr.verified")["passed"])

    def test_with_no_round_left_the_push_says_the_check_failed(self):
        rnd = self.review()
        self.settings = self.load(
            CONFIG_WITH_ROUNDS.format(rounds=0)
        )
        self.ctx = self.context()
        self.runner.verify = [self.failing()]
        self.act("fix", rnd)
        self.assertNotIn("r1-fix-2", self.runner.names())
        listed = dict(prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))[0])
        self.assertEqual(listed["push"], "Push anyway: the check left 2 item(s) open (alice/proj:greeting)")
        self.assertEqual(listed["fix"], "Send the 2 item(s) the check left open back to the fixer, then check again")
        self.act("push", rnd)  # declined
        self.assertIn("The check of the fixes left items open.", self.confirmed[-1])
        self.act("fix", rnd)  # the open items, once more
        third = json.loads((rnd.folder / "fix-findings-2.json").read_text())["findings"]
        self.assertEqual([f["id"] for f in third], ["F1", "V1-1"])
        self.assertTrue(runlog.last_event(self.run.path, "pr.verified")["passed"])

    def test_check_verify(self):
        from placido import prcheck

        path = self.tmp / "verify.json"
        path.write_text(json.dumps({"verdicts": [
            {"id": "F1", "verdict": "resolved", "note": "ok"},
            {"id": "F9", "verdict": "fixed", "note": "?"},
        ], "findings": [{"id": "V2", "area": "bugs", "severity": "minor", "size": "small",
                         "title": "t", "description": "d", "evidence": "e", "fix": "f"}]}))
        problems = prcheck.check_verify(path, ["F1", "F2"])
        self.assertIn("F2 needs exactly one verdict; it has 0.", problems)
        self.assertTrue(any("needs an id, a verdict of resolved, unresolved" in p for p in problems))
        self.assertIn("finding 1 has id 'V2'; it should be V1.", problems)


CONFIG_WITH_ROUNDS = """
[project]
base = "main"
[commands.unit]
run = "true"
[commands.lint]
run = "true"
[gates]
slice = ["unit"]
final = ["lint"]
[pr]
fix_rounds = {rounds}
"""
