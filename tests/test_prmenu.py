import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable
from unittest import mock

from placido import decisions, pr, pragents, prcheck, prgh, prmenu, runlog
from tests.test_pr import CONFIG, PRTestCase, git_commit, sh


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


class EditTest(MenuTestCase):
    def editor_used(self, environ: dict[str, str]) -> str:
        with mock.patch.dict("os.environ", environ), mock.patch.object(prmenu.subprocess, "run") as run:
            for name in ("VISUAL", "EDITOR"):
                if name not in environ:
                    prmenu.os.environ.pop(name, None)
            prmenu._edit(self.tmp / "comment.md")
        return run.call_args.args[0][0]

    def test_nano_unless_the_user_chose_an_editor(self):
        self.assertEqual(self.editor_used({}), "nano")

    def test_the_users_editor_wins(self):
        self.assertEqual(self.editor_used({"EDITOR": "vim"}), "vim")


class PostTest(MenuTestCase):
    def test_a_request_for_changes_is_posted_as_one_with_placidos_marker(self):
        rnd = self.review(verdict="request-changes", routing={"share": 0.7, "estimate": "x", "route": "contributor"})
        self.confirms = [True]
        self.act("post", rnd)
        self.assertEqual(self.confirmed, ["Post this on #7 as a review requesting changes?"])
        kind, body = self.hub.calls[-1][1:]
        self.assertEqual(kind, "request-changes")
        self.assertTrue(body.startswith("Thanks for this.\n\n1. Fix greet.py."))
        self.assertIn(prgh.marker(1, self.head), body)
        self.assertNotIn("post", self.keys(rnd))

    def test_the_comment_is_shown_as_it_will_be_posted(self):
        rnd = self.review(verdict="request-changes", routing={"share": 0.7, "estimate": "x", "route": "contributor"})
        (rnd.folder / "comment.md").write_text("Thanks.\n\n1. Changed after the summary.\n")
        shown = io.StringIO()
        with redirect_stdout(shown):
            self.act("post", rnd)  # declined
        self.assertIn("---- the comment to the contributor ----\nThanks.\n\n1. Changed after the summary.\n",
                      shown.getvalue())
        self.assertEqual(self.hub.calls, [])

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

    def write_spec(self, text: str) -> Callable[[Path], None]:
        def write(wt: Path) -> None:
            (wt / ".placido").mkdir(exist_ok=True)
            (wt / ".placido" / "spec.ts").write_text(text)
        return write

    def test_a_fix_s_change_to_a_host_path_the_user_refuses_is_sent_back(self):
        rnd = self.review()
        self.runner.during["r1-fix-1"] = self.write_spec("test('no')\n")
        self.answers = [False]
        with self.assertRaisesRegex(pr.PRStop, "the fix did not finish"):
            self.act("fix", rnd)
        self.assertIn("the user refused your change to .placido/spec.ts", self.runner.problems[-1][1][0])
        self.assertIn("dispute the findings that needed it", self.runner.problems[-1][1][0])
        self.assertEqual(self.asked, ["Have you read that change, and may it run here?"])
        self.assertEqual(runlog.last_event(self.run.path, "pr.host_change_refused")["files"], [".placido/spec.ts"])
        self.assertEqual(sh(self.worktree, "git", "status", "--porcelain"), "")  # set aside in a stash
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD"), self.head)

    def test_with_nobody_to_approve_it_a_fix_s_change_to_a_host_path_is_sent_back(self):
        rnd = self.review()
        self.runner.during["r1-fix-1"] = self.write_spec("test('no')\n")
        with self.assertRaisesRegex(pr.PRStop, "the fix did not finish"):
            self.act("fix", rnd)
        self.assertIn("nobody was at the terminal to approve", self.runner.problems[-1][1][0])

    def test_a_fix_s_change_to_a_host_path_the_user_approves_is_committed_and_trusted(self):
        rnd = self.review()
        self.runner.during["r1-fix-1"] = self.write_spec("test('declines')\n")
        self.answers = [True]
        self.act("fix", rnd)
        head = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.assertEqual(sh(self.worktree, "git", "show", "HEAD:.placido/spec.ts"), "test('declines')")
        self.assertEqual(len(self.asked), 1)
        self.assertIn("test('declines')", (rnd.folder / "host-paths-fix-1.diff").read_text())
        committed = runlog.last_event(self.run.path, "pr.host_change_committed")
        self.assertEqual((committed["commit"], committed["files"]), (head, [".placido/spec.ts"]))
        self.assertEqual(pr.trusted(self.run.path, rnd), head)
        pr.verify_host_paths(self.ctx, rnd, "the last gates")  # what comes next runs on it
        self.assertNotIn("pr.host_paths_changed", self.kinds())

    def test_an_approval_holds_for_that_content_only(self):
        rnd = self.review()
        self.write_spec("test('declines')\n")(self.worktree)
        self.answers = [True, False]
        self.assertTrue(pr.approve_host_change(self.ctx, rnd, "fix 1"))
        self.assertTrue(pr.approve_host_change(self.ctx, rnd, "fix 1"))  # not asked again
        pr.verify_host_paths(self.ctx, rnd, "the gates")
        self.write_spec("test('declines'); fetch('https://evil')\n")(self.worktree)
        with self.assertRaisesRegex(pr.PRStop, "changed since they were trusted"):
            pr.verify_host_paths(self.ctx, rnd, "the gates")
        self.assertFalse(pr.approve_host_change(self.ctx, rnd, "fix 1"))
        self.assertEqual(len(self.asked), 2)


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

    def test_pending_ci_is_waited_out_then_merged_without_asking_again(self):
        rnd, fix = self.fixed_and_pushed()
        self.hub.ci = [{"name": "unit", "bucket": "pending"}]
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        self.assertIn("Squash-merge into main once CI is green (placido waits up to 1h)", dict(listed)["merge"])
        self.confirms = [True]
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            self.hub.ci = [{"name": "unit", "bucket": "pass"}]

        with mock.patch.object(prmenu.time, "sleep", sleep):
            self.act("merge", rnd)
        self.assertEqual(len(polls), 1)
        self.assertEqual(self.confirmed[-1], f'Squash-merge #7 into main as "Add a greeting (#7)" once CI is green on'
                                             f" {fix[:12]}? Placido waits up to 1h and merges then")
        self.assertEqual(self.hub.calls[-1][:2], ("merge", fix))
        self.assertEqual([k for k in self.kinds() if k in ("pr.merge_waiting", "pr.merged")], ["pr.merge_waiting", "pr.merged"])

    def test_ci_that_goes_red_while_waited_on_merges_nothing(self):
        rnd, _ = self.fixed_and_pushed()
        self.hub.ci = [{"name": "unit", "bucket": "pending"}]
        self.confirms = [True]

        def sleep(seconds):
            self.hub.ci = [{"name": "unit", "bucket": "fail"}]

        with mock.patch.object(prmenu.time, "sleep", sleep), self.assertRaisesRegex(pr.PRStop, "CI went red on .* .unit.; nothing was merged"):
            self.act("merge", rnd)
        self.assertNotIn("merge", [c[0] for c in self.hub.calls])
        self.assertNotIn("pr.merged", self.kinds())

    def test_declining_merges_nothing_and_waits_for_nothing(self):
        rnd, _ = self.fixed_and_pushed()
        self.hub.ci = [{"name": "unit", "bucket": "pending"}]
        self.confirms = [False]
        self.act("merge", rnd)
        self.assertNotIn("merge", [c[0] for c in self.hub.calls])
        self.assertNotIn("pr.merge_waiting", self.kinds())

    def test_ci_runs_held_for_approval_are_not_waited_on(self):
        rnd, _ = self.fixed_and_pushed()
        self.hub.ci = [{"name": "unit", "bucket": "pending"}]
        self.hub.waiting = [{"databaseId": 5}]
        with self.assertRaisesRegex(pr.PRStop, "1 CI run.s. on .* wait for a maintainer's approval"):
            self.act("merge", rnd)
        self.assertNotIn("pr.merge_waiting", self.kinds())

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
        shown = io.StringIO()
        with redirect_stdout(shown):
            self.act("close", rnd)
        self.assertIn("---- the comment to the contributor ----\nThanks for this.", shown.getvalue())
        self.assertEqual(self.confirmed, ["Close #7, posting this comment?"])
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
        self.confirms = [True]
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

    def test_a_host_path_committed_by_hand_is_put_back_without_stopping_the_close(self):
        from placido import close
        from placido.herdr import Herdr

        self.review()
        (self.worktree / ".placido" / "stack.sh").write_text("echo by hand\n")
        git_commit(self.worktree, "by hand")
        settings = self.load('[project]\nteardown = "sh .placido/stack.sh > $OUT"\n')
        with mock.patch.dict("os.environ", {"OUT": str(self.tmp / "teardown.out")}):
            close.close(self.run, Herdr(self.fake), settings, force=False)
        self.assertEqual((self.tmp / "teardown.out").read_text(), "stack\n")
        self.assertEqual(runlog.last_event(self.run.path, "close.host_paths_restored")["files"], [".placido/stack.sh"])
        self.assertFalse(self.worktree.exists())

    def test_the_users_own_changes_still_stop_the_close_beside_a_restored_host_path(self):
        from placido import close
        from placido.herdr import Herdr

        self.review()
        (self.worktree / ".placido" / "stack.sh").write_text("echo by hand\n")
        git_commit(self.worktree, "by hand")
        (self.worktree / "greet.py").write_text("print('half done')\n")
        (self.worktree / "wip.txt").write_text("half done\n")
        with self.assertRaisesRegex(close.CloseError, r"uncommitted changes:\n M greet\.py\n\?\? wip\.txt\n"):
            close.close(self.run, Herdr(self.fake), self.settings, force=False)
        self.assertTrue(self.worktree.exists())

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
        self.assertIn(f"log -p --first-parent --remerge-diff {self.head[:12]}..", verify["task"])
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

    def kept_fixer(self, step: str) -> str:
        return next(e["name"] for e in runlog.read_events(self.run.path)
                    if e["event"] == "pr.kept_open" and e.get("role") == "fix" and e["step"].endswith(step))

    def test_the_fixer_is_resumed_for_what_the_check_of_its_work_left_open(self):
        rnd = self.review()
        self.runner.verify = [self.failing()]
        self.act("fix", rnd)
        fixes = [c for c in self.runner.calls if c["name"].startswith("r1-fix")]
        self.assertEqual([(c["how"], c["name"]) for c in fixes], [("start", "r1-fix-1"), ("resume", "r1-fix-2")])
        fixer = self.kept_fixer("r1-fix-1")
        self.assertEqual((fixes[0]["keep_open"], fixes[1]["agent"], fixes[1]["keep_open"]), (True, fixer, True))
        self.assertTrue(fixes[1]["task"].startswith("# The check of your fix to pull request #7 left items open"))
        self.assertIn("fix-findings-2.json", fixes[1]["task"])
        checks = [c["how"] for c in self.runner.calls if c["name"].startswith("r1-verify")]
        self.assertEqual(checks, ["start", "start"])  # each check a fresh session
        self.assertEqual(self.runner.closed.count(fixer), 1)  # once, when the fix is done
        self.assertEqual(pr.open_agents(self.run.path), [])

    def test_a_fixer_that_cannot_be_resumed_gives_way_to_a_fresh_one(self):
        rnd = self.review()
        self.runner.verify = [self.failing()]
        self.runner.failures["r1-fix-2"] = ["agent-exited"]
        self.act("fix", rnd)
        fixes = [(c["how"], c["name"]) for c in self.runner.calls if c["name"].startswith("r1-fix")]
        self.assertEqual(fixes, [("start", "r1-fix-1"), ("resume", "r1-fix-2"), ("start", "r1-fix-2")])
        self.assertNotEqual(self.kept_fixer("r1-fix-2"), self.kept_fixer("r1-fix-1"))
        self.assertIn(self.kept_fixer("r1-fix-1"), self.runner.closed)
        self.assertIn(self.kept_fixer("r1-fix-2"), self.runner.closed)
        self.assertEqual(pr.open_agents(self.run.path), [])

    def test_the_fixer_is_closed_when_its_check_stops_the_fix(self):
        rnd = self.review()
        self.runner.failures["r1-verify-1"] = ["invalid"]
        with self.assertRaisesRegex(pr.PRStop, "the check of fix 1 did not finish"):
            self.act("fix", rnd)
        self.assertIn(self.kept_fixer("r1-fix-1"), self.runner.closed)
        self.assertEqual(pr.open_agents(self.run.path), [])

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


class BaseMergeTest(MenuTestCase):
    """The base branch moved on after the pull request started from it."""

    def advance_main(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        tip = git_commit(self.repo, "later on main")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        return tip

    def fix_label(self, rnd: pr.Round) -> str:
        return dict(prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))[0])["fix"]

    def test_the_base_is_merged_in_as_its_own_commit_before_the_fix(self):
        rnd = self.review()
        tip = self.advance_main({"later.txt": "later\n", ".placido/notes/pr.md": "Run every engine, twice.\n"})
        self.assertEqual(self.fix_label(rnd), "Merge main into the branch, then fix the 1 kept finding(s) here, on the"
                         " contributor's branch, and have the fixes checked: committed locally, nothing pushed")
        self.act("fix", rnd)
        merged = runlog.last_event(self.run.path, "pr.base_merged")
        self.assertEqual((merged["tip"], merged["conflicts"]), (tip, []))
        self.assertEqual(sh(self.worktree, "git", "rev-list", "--parents", "-n1", merged["commit"]).split()[1:],
                         [self.head, tip])
        self.assertEqual(sh(self.worktree, "git", "log", "-1", "--format=%s", merged["commit"]),
                         "Merge branch 'main' into greeting")
        fix = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD^"), merged["commit"])
        # main's host paths came with the merge, and placido trusts them from then on.
        self.assertEqual((self.worktree / ".placido/notes/pr.md").read_text(), "Run every engine, twice.\n")
        fixer = next(c for c in self.runner.calls if c["name"] == "r1-fix-1")
        self.assertIn(f"placido merged main ({tip[:12]}) into the branch before you", fixer["task"])
        verify = next(c for c in self.runner.calls if c["name"] == "r1-verify-1")
        self.assertIn(f"--remerge-diff {self.head[:12]}..{fix[:12]}", verify["task"])
        self.assertNotIn("r1-merge", self.runner.names())
        listed = dict(prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))[0])
        self.assertEqual(listed["push"], "Push the 2 commit(s) made here, checked, to alice/proj:greeting")
        self.assertIn(f"Merged main ({tip[:12]}) into the branch here as {merged['commit'][:12]}\n",
                      prmenu.summary(self.ctx, rnd))
        self.confirms = [True]
        self.act("push", rnd)
        self.assertEqual(self.fork_head(), fix)

    def test_a_merge_outlives_a_fix_that_did_not_finish(self):
        rnd = self.review()
        self.advance_main({"later.txt": "later\n"})
        self.runner.failures["r1-fix-1"] = ["invalid"]
        with self.assertRaisesRegex(pr.PRStop, "the fix did not finish"):
            self.act("fix", rnd)
        merge = runlog.last_event(self.run.path, "pr.base_merged")["commit"]
        rnd = pragents.review(self.ctx)  # placido pr again: the same round, the merge kept
        self.assertEqual((rnd.number, sh(self.worktree, "git", "rev-parse", "HEAD")), (1, merge))
        self.act("fix", rnd)
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD^"), merge)
        self.assertEqual(self.kinds().count("pr.base_merged"), 1)

    def test_conflicts_are_resolved_by_the_fix_role_into_the_merge_commit(self):
        rnd = self.review()
        self.advance_main({"greet.py": "print('hello')\n"})
        self.act("fix", rnd)
        self.assertEqual(self.runner.problems, [])
        merged = runlog.last_event(self.run.path, "pr.base_merged")
        self.assertEqual(merged["conflicts"], ["greet.py"])
        self.assertEqual(sh(self.worktree, "git", "show", f"{merged['commit']}:greet.py"), "print('hi there, all')")
        resolver = next(c for c in self.runner.calls if c["name"] == "r1-merge")
        self.assertEqual(resolver["role"], "fix")
        self.assertIn("git stopped on conflicts in: greet.py.", resolver["task"])
        self.assertEqual([n for n in self.runner.names() if n.startswith(("r1-merge", "r1-fix", "r1-verify"))],
                         ["r1-merge", "r1-fix-1", "r1-verify-1"])
        self.assertIn(", resolving greet.py", prmenu.summary(self.ctx, rnd))

    def unfinished(self, rnd: pr.Round) -> list[str]:
        with self.assertRaisesRegex(pr.PRStop, "the merge of main did not finish"):
            self.act("fix", rnd)
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD"), self.head)
        self.assertEqual(sh(self.worktree, "git", "status", "--porcelain", "--untracked-files=all"), "")
        self.assertNotIn("pr.base_merged", self.kinds())
        self.assertNotIn("r1-fix-1", self.runner.names())
        return self.runner.problems[-1][1]

    def test_a_conflict_marker_left_keeps_the_merge_from_being_committed(self):
        rnd = self.review()
        self.advance_main({"greet.py": "print('hello')\n"})
        self.runner.resolution = None
        self.assertEqual(self.unfinished(rnd), ["conflict markers are left in greet.py."])
        self.assertIn("<<<<<<<", (rnd.folder / "merge-unfinished.diff").read_text())

    def test_a_resolution_that_changes_more_than_the_conflicts_is_sent_back(self):
        rnd = self.review()
        self.advance_main({"greet.py": "print('hello')\n", "later.txt": "later\n", "notes.txt": "notes\n"})

        def beyond(wt):
            (wt / "later.txt").write_text("changed\n")
            sh(wt, "git", "add", "later.txt")
            (wt / "notes.txt").write_text("changed\n")
            (wt / "new.txt").write_text("new\n")

        self.runner.during["r1-merge"] = beyond
        problems = self.unfinished(rnd)
        self.assertTrue(problems[0].startswith("only the conflicts: later.txt, new.txt, notes.txt merged cleanly"),
                        problems)
        self.assertIn("--- untracked new.txt\nnew\n", (rnd.folder / "merge-unfinished.diff").read_text())

    def test_a_resolution_must_leave_the_host_paths_as_the_base_has_them(self):
        rnd = self.review()
        self.advance_main({"greet.py": "print('hello')\n"})

        def host(wt):
            (wt / ".placido" / "stack.sh").write_text("evil\n")
            (wt / ".claude").mkdir()
            (wt / ".claude" / "settings.json").write_text("{}\n")

        self.runner.during["r1-merge"] = host
        problems = self.unfinished(rnd)
        self.assertTrue(problems[-1].startswith(
            ".claude/settings.json, .placido/stack.sh run on this machine and must stay as main has"), problems)
        self.assertEqual((self.worktree / ".placido" / "stack.sh").read_text(), "echo stack\n")
        self.assertFalse((self.worktree / ".claude").exists())

    def test_a_merge_committed_by_the_resolver_is_refused(self):
        rnd = self.review()
        self.advance_main({"greet.py": "print('hello')\n"})

        def commits(wt):
            (wt / "greet.py").write_text("print('both')\n")
            git_commit(wt, "merged it myself")

        self.runner.during["r1-merge"] = commits
        self.assertIn("the merge is no longer in progress", self.unfinished(rnd)[0])

    def test_a_worktree_with_changes_is_not_merged_into(self):
        rnd = self.review()
        self.advance_main({"later.txt": "later\n"})
        (self.worktree / "stray.txt").write_text("mine\n")
        with self.assertRaisesRegex(pr.PRStop, "has uncommitted changes"):
            self.act("fix", rnd)
        self.assertEqual((self.worktree / "stray.txt").read_text(), "mine\n")
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD"), self.head)

    def test_a_pull_request_that_changes_host_paths_is_fixed_on_its_own_base(self):
        head = self.push_new_head({".placido/stack.sh": "echo theirs\n"}, self.head)
        self.answers = [True]
        rnd = self.review()
        self.advance_main({"later.txt": "later\n"})
        self.assertTrue(self.fix_label(rnd).startswith("Fix the 1 kept finding(s)"))
        self.act("fix", rnd)
        self.assertEqual(runlog.last_event(self.run.path, "pr.base_merge_skipped")["reason"], "host paths")
        self.assertNotIn("pr.base_merged", self.kinds())
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD^"), head)

    def test_a_branch_whose_fix_changed_host_paths_is_fixed_on_its_own_base(self):
        rnd = self.review()
        self.runner.during["r1-fix-1"] = lambda wt: (wt / ".placido" / "stack.sh").write_text("echo approved\n")
        self.answers = [True]
        self.act("fix", rnd)
        fixed = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.advance_main({"later.txt": "later\n"})
        self.assertFalse(prmenu.state(self.ctx, rnd).merges_base)
        self.act("fix", rnd)
        self.assertEqual(runlog.last_event(self.run.path, "pr.base_merge_skipped")["reason"], "host paths")
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD^"), fixed)

    def test_an_up_to_date_branch_is_fixed_without_a_merge(self):
        rnd = self.review()
        self.assertTrue(self.fix_label(rnd).startswith("Fix the 1 kept finding(s)"))
        self.act("fix", rnd)
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD^"), self.head)
        self.assertNotIn("pr.base_merged", self.kinds())

    def test_close_keeps_the_host_paths_the_merge_brought(self):
        from placido import close
        from placido.herdr import Herdr

        rnd = self.review()
        self.advance_main({".placido/notes/pr.md": "Run every engine, twice.\n"})
        self.act("fix", rnd)
        close.close(self.run, Herdr(self.fake), self.settings, force=False)
        self.assertNotIn("close.host_paths_restored", self.kinds())
        self.assertFalse(self.worktree.exists())


class LastGatesTest(MenuTestCase):
    """The project's last gates, which an issue run keeps for after the review, run on
    demand from the menu."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = self.load(CONFIG.replace('final = ["lint"]', 'final = ["lint"]\nlast = ["e2e"]') + (
            '[commands.e2e]\nrun = "echo the browser suite ran; '
            '[ -z \\"$E2E_TOUCH\\" ] || echo x >> greet.py; exit ${E2E_EXIT:-0}"\n'
        ))
        self.ctx = self.context()

    def local_head(self) -> str:
        return sh(self.worktree, "git", "rev-parse", "HEAD")

    def run_last(self, rnd: pr.Round) -> str:
        shown = io.StringIO()
        with redirect_stdout(shown):
            self.act("last", rnd)
        return shown.getvalue()

    def test_they_run_on_the_worktree_as_it_is_and_say_how_it_went(self):
        rnd = self.review()
        self.act("fix", rnd)
        fix = self.local_head()
        s = prmenu.state(self.ctx, rnd)
        self.assertEqual(prmenu._last_text(self.ctx, s), " · last gates not run")
        listed, _ = prmenu.options(self.ctx, rnd, s)
        self.assertEqual([k for k, _ in listed], ["edit", "edit-merge", "last", "push", "post", "close", "quit"])
        self.assertEqual(dict(listed)["last"], f"Run the last gates (e2e) on {fix[:12]}")
        shown = self.run_last(rnd)
        self.assertIn("the browser suite ran\n", shown)
        self.assertIn(f"the last gates passed on {fix[:12]}", shown)
        self.assertEqual((rnd.folder / "last-gates" / "1-e2e.log").read_text(), "the browser suite ran\n")
        ran = runlog.last_event(self.run.path, "pr.last_gates")
        self.assertEqual((ran["head"], ran["results"]), (fix, {"e2e": 0}))
        self.assertEqual(prmenu._last_text(self.ctx, prmenu.state(self.ctx, rnd)), f" · last gates passed on {fix[:12]}")
        self.assertIn(f"Last gates on {fix[:12]}: e2e pass", prmenu.summary(self.ctx, rnd))

    def test_a_failing_one_stops_the_push_picked_with_it(self):
        rnd = self.review()
        self.act("fix", rnd)
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        numbers = {key: str(n) for n, (key, _) in enumerate(listed, 1)}
        answers = iter([f"{numbers['push']} {numbers['last']}", ""])
        self.confirms = [True]
        with mock.patch.dict("os.environ", {"E2E_EXIT": "3"}), redirect_stdout(io.StringIO()):
            prmenu.menu(self.ctx, rnd, lambda prompt: next(answers), self.confirm)
        self.assertEqual(self.fork_head(), self.head)  # the push never ran
        failed = runlog.last_event(self.run.path, "pr.action_failed")
        self.assertEqual(failed["action"], "last")
        self.assertIn("e2e (exit 3)", failed["error"])
        self.assertTrue(prmenu._last_text(self.ctx, prmenu.state(self.ctx, rnd)).startswith(" · last gates failed (e2e)"))

    def test_a_host_path_changed_since_it_was_trusted_stops_them_before_they_run(self):
        rnd = self.review()
        (self.worktree / ".placido" / "stack.sh").write_text("echo evil\n")
        with self.assertRaisesRegex(pr.PRStop, r"\.placido/stack\.sh changed since they were trusted"):
            self.run_last(rnd)
        self.assertFalse((rnd.folder / "last-gates" / "1-e2e.log").exists())

    def test_what_they_change_in_the_worktree_is_put_back(self):
        rnd = self.review()
        with mock.patch.dict("os.environ", {"E2E_TOUCH": "1"}):
            self.run_last(rnd)
        self.assertEqual(sh(self.worktree, "git", "status", "--porcelain"), "")
        self.assertIn("the-e2e-gate", " ".join(e.get("evidence", "") for e in runlog.read_events(self.run.path)
                                               if e["event"] == "pr.tree_restored"))

    def test_on_a_base_that_moved_on_they_say_whose_base_they_run_on(self):
        rnd = self.review()
        (self.repo / "later.txt").write_text("later\n")
        git_commit(self.repo, "later on main")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        label = dict(prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))[0])["last"]
        self.assertTrue(label.endswith(": main has moved on, and this runs on the pull request's own base"), label)

    def test_a_project_without_them_is_offered_none(self):
        self.settings = self.load(CONFIG)
        self.ctx = self.context()
        rnd = self.review()
        s = prmenu.state(self.ctx, rnd)
        self.assertNotIn("last", [k for k, _ in prmenu.options(self.ctx, rnd, s)[0]])
        self.assertEqual(prmenu._last_text(self.ctx, s), "")


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


TWO = [
    {"question": "Is the first option enough?", "recommendation": "Accept it."},
    {"question": "Widen the guard, or narrow its promise?", "recommendation": "Widen it."},
]


def answer(n: int, **overrides) -> dict:
    return {"question": f"question {n}", "decision": "narrow it", "why": "less code", "as_recommended": False,
            **overrides}


class DecisionsTest(MenuTestCase):
    """The judge's open questions, which the user decides before the menu, every one."""

    def final(self, rnd: pr.Round) -> dict:
        return json.loads((rnd.folder / "final.json").read_text())

    def notes(self) -> list[tuple[str, str]]:
        return [(argv[3], argv[5]) for argv in self.fake.calls if argv[1:3] == ["notification", "show"]]

    def test_every_open_question_is_put_to_the_user_before_the_menu(self):
        rnd = self.review(decisions=TWO)
        self.assertEqual(self.runner.problems, [])
        self.assertEqual(self.runner.names()[-2:], ["r1-judge", "r1-decide"])
        call = self.runner.calls[-1]
        self.assertTrue(call["interactive"])
        self.assertEqual(call["spec"], self.settings.roles["decide"].agent)
        self.assertIn("pr-decide", call["task"])
        self.assertIn("its 2 decision(s) for the maintainer", call["task"])
        self.assertIn("decided.json", call["task"])
        self.assertEqual([k for k in self.kinds() if k.startswith("pr.")][-3:], ["pr.judged", "pr.ready", "pr.decided"])
        step = runlog.last_event(self.run.path, "pr.decided")["step"]
        self.assertEqual(decisions.entries(self.run.path), [(1, step), (2, step)])
        self.assertTrue(all(d["decided"]["as_recommended"] for d in self.final(rnd)["decisions"]))
        self.assertIn("**Decided (as recommended):** Accept it.", (rnd.folder / "final.md").read_text())
        text = prmenu.summary(self.ctx, rnd)
        self.assertIn(f"Your decisions (in {decisions.path(self.run.path)}):", text)
        self.assertIn("  1. question 1 → Accept it. (as recommended)", text)
        self.assertNotIn("Decisions for you", text)
        self.assertIn(("placido · pr-7: the review is ready, with decisions for you",
                       "#7: fix-here; 2 decision(s) wait for you in the r1-decide tab; placido pr 7"), self.notes())

    def test_a_review_without_open_questions_asks_nothing(self):
        rnd = self.review()
        self.assertNotIn("r1-decide", self.runner.names())
        self.assertNotIn("pr.decided", self.kinds())
        self.assertIn(("placido · pr-7: the review is ready", "#7: fix-here; placido pr 7"), self.notes())
        self.assertEqual(decisions.entries(self.run.path), [])
        self.assertIn("fix", self.keys(rnd))

    def test_a_decision_against_the_recommendation_is_what_the_fix_and_its_check_work_from(self):
        self.runner.decided = {"answers": [
            answer(1, decision="Accept it.", as_recommended=True),
            answer(2, fixes={"F1": "Narrow the doc comment to a later statement."}),
        ], "verdict": "fix-here"}
        self.runner.drafts = {"comment.md": "Thanks.\n\n1. Narrowed the guard's promise.\n",
                              "merge.md": "Close each response (#7)\n\nNarrowed.\n"}
        rnd = self.review(decisions=TWO)
        f1 = self.final(rnd)["findings"][0]
        self.assertEqual((f1["fix"], f1["decided_by"], f1["decision"]), ("Narrow the doc comment to a later statement.", 2, "keep"))
        judged = runlog.last_event(self.run.path, "pr.judged")["step"]
        self.assertEqual(json.loads((self.run.path / "steps" / judged / "final.json").read_text())["findings"][0]["fix"], "f")
        self.assertEqual((rnd.folder / "comment.md").read_text(), "Thanks.\n\n1. Narrowed the guard's promise.\n")
        self.assertEqual((rnd.folder / "merge.md").read_text(), "Close each response (#7)\n\nNarrowed.\n")
        decided = runlog.last_event(self.run.path, "pr.decided")
        self.assertEqual((decided["as_recommended"], decided["changed"], decided["dropped"], decided["added"]),
                         ([True, False], ["F1"], [], []))
        self.assertIn("**Fix (your decision 2):** Narrow the doc comment", (rnd.folder / "final.md").read_text())
        text = prmenu.summary(self.ctx, rnd)
        self.assertIn("  2. question 2 → narrow it (the recommendation was: Widen it.)", text)
        self.assertIn("the greeting [A1, B1: all] (fix: your decision 2)", text)
        self.assertIn("1. Narrowed the guard's promise.", text)
        self.act("fix", rnd)
        listed = json.loads((rnd.folder / "fix-findings-1.json").read_text())["findings"]
        self.assertEqual(listed[0]["fix"], "Narrow the doc comment to a later statement.")
        for name in ("r1-fix-1", "r1-verify-1"):
            task = next(c["task"] for c in self.runner.calls if c["name"] == name)
            self.assertIn(f"{decisions.path(self.run.path)}. Read them; where one differs from the review, the"
                          " decision wins.", task)

    def test_a_decision_can_drop_a_finding_add_work_and_send_it_back_to_the_contributor(self):
        added = {"title": "close in the step helpers", "area": "gaps", "severity": "significant", "size": "medium",
                 "location": "steps.go:9", "description": "each step keeps its connection", "fix": "close it first",
                 "id": "X1"}
        self.runner.decided = {"answers": [answer(1, drop=["F1"], add=[added]), answer(2, as_recommended=True)],
                               "verdict": "request-changes"}
        self.runner.drafts = {"comment.md": "Thanks.\n\n1. Please close in the step helpers.\n"}
        rnd = self.review(decisions=TWO)
        final = self.final(rnd)
        self.assertEqual((final["verdict"], final["routing"]["route"], final["routing"]["judge_verdict"]),
                         ("request-changes", "contributor", "fix-here"))
        f1, f2 = final["findings"]
        self.assertEqual((f1["decision"], f1["reason"]), ("drop", "Your decision 1: narrow it"))
        self.assertEqual((f2["id"], f2["sources"], f2["agreement"], f2["decision"], f2["fix"], f2["decided_by"]),
                         ("F2", ["decision 1"], "yours", "keep", "close it first", 1))
        decided = runlog.last_event(self.run.path, "pr.decided")
        self.assertEqual((decided["verdict"], decided["changed"], decided["dropped"], decided["added"]),
                         ("request-changes", [], ["F1"], ["F2"]))
        self.assertFalse((rnd.folder / "merge.md").exists())
        self.assertIn("# Verdict: request-changes (yours; the judge's was fix-here)", (rnd.folder / "final.md").read_text())
        text = prmenu.summary(self.ctx, rnd)
        self.assertIn("Verdict: request-changes (yours; the judge's was fix-here). It greets", text)
        self.assertIn("Fixes: the contributor, as you decided (about 10% of the pull request", text)
        self.assertIn("F2  significant gaps        `steps.go:9` close in the step helpers [decision 1: yours]\n", text)
        self.assertIn("F1  the greeting [A1, B1]: Your decision 1: narrow it", text)
        self.assertEqual(self.keys(rnd), ["edit", "fix", "post", "close", "quit"])
        self.confirms = [True]
        self.act("post", rnd)
        self.assertEqual(self.hub.calls[-1][:2], ("review", "request-changes"))

    def test_questions_left_unanswered_are_asked_again_until_they_are_answered(self):
        self.runner.failures["r1-decide"] = ["agent-exited"]
        with self.assertRaisesRegex(pr.PRStop, "the decisions were not all taken .agent-exited.: placido pr 7 asks"
                                               " them again"):
            self.review(decisions=TWO)
        self.assertNotIn("pr.decided", self.kinds())
        self.assertEqual(decisions.entries(self.run.path), [])
        pragents.review(self.ctx)
        pragents.review(self.ctx)
        self.assertEqual(self.runner.names().count("r1-decide"), 2)
        self.assertEqual((self.kinds().count("pr.decided"), self.kinds().count("pr.ready")), (1, 1))
        self.assertEqual(len(decisions.entries(self.run.path)), 2)

    def test_a_later_rounds_reviewers_and_judge_are_told_what_was_decided(self):
        self.review(decisions=TWO)
        self.runner.final = None
        self.push_new_head({"greet.py": "print('hi there, all')\n"}, self.head)
        self.runner.earlier = ["F1"]
        pragents.review(self.ctx)
        for name in ("r2-review-a", "r2-review-b", "r2-judge"):
            task = next(c["task"] for c in self.runner.calls if c["name"] == name)
            self.assertIn(f"{decisions.path(self.run.path)}. Read them;", task)
        self.assertNotIn("r2-decide", self.runner.names())

    def test_the_judge_writes_each_open_question_with_its_recommendation(self):
        with self.assertRaisesRegex(pr.PRStop, "the judge did not finish"):
            self.review(decisions=["Accept it?"])
        self.assertIn("decision 1 needs a non-empty question and recommendation.", self.runner.problems[0][1])

    def test_check_decided(self):
        final = {**self.runner.default_final(), "decisions": TWO}
        folder = self.tmp / "decided"
        folder.mkdir()
        (folder / "comment.md").write_text("Thanks.\n")
        (folder / "merge.md").write_text("Add a greeting (#7)\n")
        good = {"answers": [answer(1), answer(2)], "verdict": "fix-here"}

        def problems(**changes) -> list[str]:
            data = json.loads(json.dumps(good))
            for key, value in changes.items():
                if key[1:].isdigit():  # a1, a2: changes to that answer
                    data["answers"][int(key[1:]) - 1].update(value)
                else:
                    data[key] = value
            (folder / "decided.json").write_text(json.dumps(data))
            return prcheck.check_decided(folder, final)

        self.assertEqual(problems(), [])
        self.assertEqual(problems(answers=[answer(1)]), [
            "the final review has 2 decision(s), and decided.json answers 1: one answer per decision, in the final"
            " review's order."])
        self.assertEqual(problems(a1={"why": " "}), ["answer 1 needs a non-empty question, decision, and why."])
        self.assertEqual(problems(a2={"as_recommended": "yes"}), ['answer 2 needs "as_recommended": true or false.'])
        self.assertEqual(problems(a1={"fixes": {"F9": "x"}}), ["answer 1 names 'F9', which is no kept finding (F1)."])
        self.assertEqual(problems(a1={"fixes": {"F1": " "}}), ['answer 1: "fixes" maps a kept finding\'s id to its fix as decided.'])
        self.assertEqual(problems(a1={"fixes": {"F1": "x"}}, a2={"drop": ["F1"]}),
                         ["F1 is changed twice; say in one answer what becomes of it."])
        self.assertEqual(problems(a1={"add": [{"title": "t", "area": "gaps", "severity": "huge", "size": "small",
                                               "description": "d", "fix": "f"}]}),
                         ["answer 1's added finding 1 has severity 'huge'; use blocking, significant, minor."])
        self.assertEqual(problems(verdict="ship"), ["verdict 'ship': use merge, fix-here, request-changes, close."])
        self.assertEqual(problems(verdict="merge"), ['a "merge" verdict fixes nothing, but F1 are still kept: drop'
                                                     ' them, or keep the verdict that fixes them.'])
        self.assertEqual(problems(a1={"drop": ["F1"]}),
                         ['no finding is left to fix, so the verdict is "merge" or "close", not "fix-here".'])
        self.assertEqual(problems(a1={"drop": ["F1"]}, verdict="merge"), [])
        self.assertEqual(problems(verdict="request-changes", a1={"drop": ["F1"], "add": [
            {"title": "t", "area": "gaps", "severity": "minor", "size": "small", "description": "d", "fix": "f"}]}), [])
        (folder / "merge.md").write_text("A" * 73 + "\n")
        self.assertEqual(problems(), ["the squash subject is 73 characters; keep it to 72."])
        self.assertEqual(problems(verdict="close"), [])  # nothing is merged, so no squash message
        (folder / "comment.md").write_text(" \n")
        self.assertEqual(problems(verdict="close"), [f"{folder / 'comment.md'} is missing or empty."])
