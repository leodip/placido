import io
import json
import shlex
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Callable
from unittest import mock

from placido import decisions, pr, pragents, prcheck, prgh, prmenu, runlog
from tests.test_pr import CONFIG, PRTestCase, finding, git_commit, sh


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

    def act(self, key: str, rnd: pr.Round) -> pr.Round | None:
        """The action, as the menu runs it; the round it ended in, for a fix or a review."""

        return prmenu.ACTIONS[key](self.ctx, rnd, prmenu.state(self.ctx, rnd), self.confirm)

    def fixed(self) -> pr.Round:
        """Round 1 reviewed, its finding fixed here, and the branch with the fix reviewed in
        full in round 2, which keeps nothing."""

        return self.act("fix", self.review())

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
        self.assertNotIn("Fixes:", text)
        self.assertIn("Scan: clear\n", text)
        self.assertIn("F1  significant bugs        `greet.py:1` the greeting [A1, B1: all]", text)
        self.assertIn("Found outside this change (for you only, never in the comment):\n  - old bug (x.py) [security]", text)
        self.assertIn("Gates: unit pass · lint pass", text)
        self.assertIn("Reviewers: A claude opus xhigh (1 findings); B codex gpt-6.1-sol max (1 findings)", text)
        self.assertIn("---- the comment to the contributor ----\nThanks for this.", text)


class OptionsTest(MenuTestCase):
    def test_after_a_fix_here_review(self):
        rnd = self.review()
        self.assertEqual(self.keys(rnd), ["edit", "edit-merge", "fix", "post", "ask", "close", "quit"])

    def test_merge_is_offered_when_nothing_is_kept(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        self.assertIn("merge", self.keys(rnd))
        self.assertNotIn("fix", self.keys(rnd))

    def test_waiting_ci_runs_can_be_approved(self):
        rnd = self.review()
        self.hub.waiting = [{"databaseId": 5, "conclusion": "action_required"}]
        self.assertIn("ci", self.keys(rnd))

    def test_a_merged_pull_request_says_how_to_end_the_run_and_asks_nothing(self):
        rnd = self.review()
        self.hub.pull = self.pull(self.head, state="MERGED")
        out = io.StringIO()
        with redirect_stdout(out):
            prmenu.menu(self.ctx, rnd, lambda prompt: self.fail(f"asked: {prompt}"), self.confirm)
        self.assertEqual(out.getvalue(), (
            f"\nThe pull request is merged. To end the run, in {self.repo.name}'s workspace (not this one):\n"
            "  placido close pr-7\n"
        ))


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
    def test_a_fix_here_comment_is_posted_as_a_comment_with_placidos_marker(self):
        rnd = self.review()
        self.confirms = [True]
        self.act("post", rnd)
        self.assertEqual(self.confirmed, ["Post this on #7 as a comment?"])
        kind, body = self.hub.calls[-1][1:]
        self.assertEqual(kind, "comment")
        self.assertTrue(body.startswith("Thanks for this.\n\n1. Fix greet.py."))
        self.assertIn(prgh.marker(1, self.head), body)
        self.assertNotIn("post", self.keys(rnd))

    def test_the_comment_is_shown_as_it_will_be_posted(self):
        rnd = self.review()
        (rnd.folder / "comment.md").write_text("Thanks.\n\n1. Changed after the summary.\n")
        shown = io.StringIO()
        with redirect_stdout(shown):
            self.act("post", rnd)  # declined
        self.assertIn("---- the comment to the contributor ----\nThanks.\n\n1. Changed after the summary.\n",
                      shown.getvalue())
        self.assertEqual(self.hub.calls, [])

    def test_a_comment_on_changes_not_pushed_yet_says_so(self):
        reviewed = self.fixed()
        with redirect_stdout(io.StringIO()):
            self.act("post", reviewed)
        self.assertEqual(self.hub.calls, [])
        self.assertEqual(self.confirmed[0], "Post this on #7 as an approving review? It may say what was changed on"
                                            " the branch, which is not pushed yet.")

    def test_a_review_that_asked_the_contributor_for_changes_reads_as_fixed_here(self):
        rnd = self.review()
        data = json.loads((rnd.folder / "final.json").read_text())
        data.update(verdict="request-changes", routing={"share": 0.7, "estimate": "x", "route": "contributor"})
        (rnd.folder / "final.json").write_text(json.dumps(data))
        listed, notes = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        self.assertIn("fix", dict(listed))
        self.assertEqual(dict(listed)["post"], "Post the comment as a comment")
        self.assertIn("its comment asks the contributor for them", notes[0])
        self.assertIn("Verdict: fix-here. It greets", prmenu.summary(self.ctx, rnd))


class FixAndPushTest(MenuTestCase):
    def test_the_fix_is_reviewed_in_full_then_pushed_only_when_confirmed(self):
        rnd = self.review()
        reviewed = self.act("fix", rnd)
        fix = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.assertNotEqual(fix, self.head)
        message = sh(self.worktree, "git", "log", "-1", "--format=%B")
        self.assertTrue(message.startswith("Greet properly\n\nThe review asked for it."))
        self.assertIn("Placido-PR-Round: 1", message)
        self.assertEqual(runlog.last_event(self.run.path, "pr.fixed")["commit"], fix)
        self.assertEqual((reviewed.number, reviewed.head, reviewed.here, reviewed.previous), (2, fix, True, 1))
        self.assertEqual(self.runner.names()[6:], ["r1-fix-1", "r2-review-a", "r2-review-b", "r2-crosscheck-a",
                                                   "r2-crosscheck-b", "r2-judge"])
        self.assertEqual(self.keys(reviewed), ["edit", "edit-merge", "push", "post", "ask", "close", "quit"])
        self.assertEqual(dict(prmenu.options(self.ctx, reviewed, prmenu.state(self.ctx, reviewed))[0])["push"],
                         "Push the 1 commit(s) made here, reviewed in full in round 2, to alice/proj:greeting")

        self.act("push", reviewed)  # declined
        self.assertEqual(self.fork_head(), self.head)
        self.assertEqual(self.confirmed[-1], "Push 1 commit(s) to alice/proj:greeting, reviewed in full in round 2?"
                                             " (never forced)")
        self.confirms = [True]
        self.act("push", reviewed)
        self.assertEqual(self.fork_head(), fix)
        self.assertEqual(runlog.last_event(self.run.path, "pr.pushed")["commit"], fix)

    def test_a_push_never_overwrites_what_the_contributor_pushed_meanwhile(self):
        rnd = self.fixed()
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
        rnd = self.fixed()
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
        reviewed = self.act("fix", rnd)
        head = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.assertEqual(sh(self.worktree, "git", "show", "HEAD:.placido/spec.ts"), "test('declines')")
        self.assertEqual(len(self.asked), 1)  # the round reviewing the fix trusts what the user approved
        self.assertEqual(runlog.last_event(self.run.path, "pr.trust_approved")["carried"], 1)
        self.assertEqual(pr.trusted(self.run.path, reviewed), head)
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
        rnd = self.fixed()
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

    def test_a_push_over_the_head_while_ci_runs_ends_the_wait_at_once(self):
        rnd, fix = self.fixed_and_pushed()
        self.hub.ci = [{"name": "unit", "bucket": "pending"}]
        self.confirms = [True]
        polls = []

        def sleep(seconds):  # time passes only here, so a wait that went on would end at its limit
            polls.append(seconds)
            self.hub.pull = self.pull("f" * 40)  # pushed over, from anywhere

        clock = lambda: float(sum(polls))  # noqa: E731
        with mock.patch.object(prmenu.time, "sleep", sleep), mock.patch.object(prmenu.time, "monotonic", clock), \
                self.assertRaisesRegex(pr.PRStop, f"^the pull request moved on from {fix[:12]} while CI ran;"
                                                  " nothing was merged.$"):
            self.act("merge", rnd)
        self.assertEqual(len(polls), 1)
        self.assertNotIn("merge", [c[0] for c in self.hub.calls])

    def test_findings_kept_that_are_not_minor_are_never_merged(self):
        rnd = self.review()  # the head reviewed, nothing made here, F1 kept
        self.assertNotIn("merge", self.keys(rnd))
        with self.assertRaisesRegex(pr.PRStop, "nothing is merged: round 1 kept F1, which are not minor"):
            self.act("merge", rnd)
        self.assertEqual(self.hub.calls, [])

    def test_no_ci_asks_first(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        self.act("merge", rnd)
        self.assertIn("CI shows no result", self.confirmed[0])
        self.assertEqual(self.hub.calls, [])


class CloseAndCITest(MenuTestCase):
    def test_close_posts_the_comment_then_closes(self):
        rnd = self.review(verdict="close")
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
        rnd = self.review()
        self.hub.waiting = [{"databaseId": 9}]
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        numbers = {key: str(n) for n, (key, _) in enumerate(listed, 1)}
        answers = iter([f"{numbers['ci']} {numbers['post']}", ""])
        self.confirms = [True]
        prmenu.menu(self.ctx, rnd, lambda prompt: next(answers), self.confirm)
        self.assertEqual([c[0] for c in self.hub.calls], ["review", "approve"])

    def test_a_fix_goes_on_with_the_round_that_reviewed_it(self):
        rnd = self.review()
        listed, _ = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        numbers = {key: str(n) for n, (key, _) in enumerate(listed, 1)}
        answers = iter([f"{numbers['fix']} {numbers['post']}", ""])
        shown = io.StringIO()
        with redirect_stdout(shown):
            prmenu.menu(self.ctx, rnd, lambda prompt: next(answers), self.confirm)
        fix = sh(self.worktree, "git", "rev-parse", "HEAD")
        self.assertIn(f"round 2, head {fix[:12]}", shown.getvalue())
        self.assertIn(f"Reviewed here: the branch with the 1 commit(s) made here since round 1's review, on"
                      f" {self.head[:12]}, the contributor's head", shown.getvalue())
        self.assertIn("Scan: clear (round 1's)", shown.getvalue())
        self.assertEqual(self.hub.calls, [])  # the post picked with the fix waits for the menu on round 2
        self.assertIn(f"Push the 1 commit(s) made here, reviewed in full in round 2", shown.getvalue())

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


class YoursTest(MenuTestCase):
    """The user's own commits in the worktree are trusted: they go out with the head the
    latest round reviewed, and no round reviews them (decided 2026-10-21)."""

    def mine(self, name: str = "mine.txt") -> str:
        (self.worktree / name).parent.mkdir(parents=True, exist_ok=True)
        (self.worktree / name).write_text("mine\n")
        return git_commit(self.worktree, "mine")

    def listed(self, rnd: pr.Round) -> tuple[dict[str, str], list[str]]:
        with redirect_stdout(io.StringIO()):
            listed, notes = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        return dict(listed), notes

    def menu_picks(self, rnd: pr.Round, key: str, meanwhile: Callable[[], None]) -> str:
        """The menu shown, something done meanwhile, then key picked from it."""

        listed, _ = self.listed(rnd)
        picks = iter([str(list(listed).index(key) + 1), None])
        shown = io.StringIO()

        def pick(prompt: str) -> str | None:
            answer = next(picks)
            if answer is not None:
                meanwhile()
            return answer

        with redirect_stdout(shown):
            prmenu.menu(self.ctx, rnd, pick, self.confirm)
        return shown.getvalue()

    def test_your_commit_goes_out_with_the_reviewed_head(self):
        reviewed = self.fixed()
        mine = self.mine()
        listed, notes = self.listed(reviewed)
        self.assertIn(f"Your 1 commit(s) in the worktree, up to {mine[:12]}, are trusted: they go out with the head"
                      " round 2 reviewed.", notes)
        self.assertNotIn("review", listed)
        self.assertRegex(listed["push"], r"^Push the \d commit\(s\) made here, reviewed in full in round 2, with your"
                                         r" 1 own commit\(s\) on it, trusted, to ")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            pr.PRStop, r"^the pull request lacks \d commit\(s\) made here: push them first$"
        ):
            self.act("merge", reviewed)
        self.confirms = [True]
        with redirect_stdout(io.StringIO()):
            self.act("push", reviewed)
        self.assertEqual(self.fork_head(), mine)
        self.publish(mine)
        self.hub.ci = [{"name": "unit", "bucket": "pass"}]
        self.assertIn(f"pinned to {mine[:12]}", self.listed(reviewed)[0]["merge"])
        self.confirms = [True]
        with redirect_stdout(io.StringIO()):
            self.act("merge", reviewed)
        self.assertEqual(self.hub.calls[-1][:2], ("merge", mine))
        self.assertEqual([r.number for r in pr.rounds(self.run.path)], [1, 2])

    def test_a_merge_picked_after_your_push_takes_your_commit(self):
        """#619: the menu showed the merge; the ask tab committed and pushed; then the user
        picked the merge from what the menu showed."""

        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        self.hub.ci = [{"name": "unit", "bucket": "pass"}]
        self.confirms = [True]
        mine: list[str] = []
        self.menu_picks(rnd, "merge", lambda: (mine.append(self.mine()), self.publish(mine[0])))
        self.assertEqual(self.hub.calls[-1][:2], ("merge", mine[0]))

    def test_a_merge_picked_after_a_contributor_push_merges_nothing(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        self.hub.ci = [{"name": "unit", "bucket": "pass"}]
        self.confirms = [True]

        def contributor_pushes() -> None:
            sh(self.repo, "git", "checkout", "-q", "-B", "late", self.head)
            (self.repo / "late.txt").write_text("late\n")
            late = git_commit(self.repo, "late")
            sh(self.repo, "git", "checkout", "-q", "main")
            self.publish(late)

        shown = self.menu_picks(rnd, "merge", contributor_pushes)
        self.assertNotIn("merge", [c[0] for c in self.hub.calls])
        self.assertIn("which this review did not cover", shown)

    def test_a_commit_off_the_reviewed_head_is_not_pushed(self):
        reviewed = self.fixed()
        sh(self.worktree, "git", "reset", "-q", "--hard", self.head)
        self.mine()
        with self.assertRaisesRegex(pr.PRStop, "nothing is pushed: the worktree is at .*, not at .*, the head round 2"
                                               " reviewed, nor on it with only your own commits"):
            self.act("push", reviewed)

    def test_a_commit_of_placidos_that_no_round_reviewed_is_not_pushed(self):
        self.runner.findings = {"A": [], "B": []}
        rnd = self.review()
        (self.worktree / "base.txt").write_text("the base, merged in\n")
        merged = git_commit(self.worktree, "merge the base")
        self.run.event("pr.base_merged", round=rnd.number, commit=merged)  # its fix then failed
        with self.assertRaisesRegex(pr.PRStop, "nothing is pushed: the worktree is at .*, nor on it with only your"
                                               " own commits"):
            self.act("push", rnd)

    def test_a_fix_goes_on_from_your_commits(self):
        rnd = self.review()  # F1 kept
        mine = self.mine(".placido/notes/pr.md")  # a host path: the fix checks them before it runs
        with redirect_stdout(io.StringIO()):
            again = self.act("fix", rnd)
        self.assertEqual(again.number, 2)
        self.assertTrue(pr.is_ancestor(self.worktree, mine, again.head))
        self.assertEqual(runlog.last_event(self.run.path, "pr.adopted")["commits"], [mine])

    def test_a_fix_no_round_reviewed_yet_is_not_pushed(self):
        rnd = self.review()  # F1 kept
        (self.worktree / "greet.py").write_text("print('fixed')\n")
        fix = git_commit(self.worktree, "fix")
        self.run.event("pr.fixed", round=rnd.number, commit=fix, step="x")  # its review then failed
        listed, _ = self.listed(rnd)
        self.assertEqual(listed["review"], "Review the branch with the fix in full, in round 2: every reviewer,"
                                           " the cross-checks, and the judge")
        with self.assertRaisesRegex(pr.PRStop, "nothing is pushed: the changes made here since round 1's review wait"
                                               " for a full review of their own"):
            self.act("push", rnd)

    def test_uncommitted_changes_are_noted(self):
        rnd = self.review()
        (self.worktree / "greet.py").write_text("half done\n")
        _, notes = prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))
        self.assertIn("The worktree has uncommitted changes: commit them to have them go out", notes[0])


class FixLoopTest(MenuTestCase):
    """A fix and the full review of it, again, until a round keeps nothing."""

    def notes(self) -> list[tuple[str, str]]:
        return [(argv[3], argv[5]) for argv in self.fake.calls if argv[1:3] == ["notification", "show"]]

    def test_what_a_round_keeps_is_fixed_and_reviewed_again(self):
        self.runner.findings_in = {2: {"A": [finding("A", 1, title="the fix broke es-MX")], "B": []}}
        rnd = self.review()
        reviewed = self.act("fix", rnd)
        self.assertEqual(self.runner.names()[6:], [
            "r1-fix-1", "r2-review-a", "r2-review-b", "r2-crosscheck-a", "r2-crosscheck-b", "r2-judge",
            "r2-fix-1", "r3-review-a", "r3-review-b", "r3-crosscheck-a", "r3-crosscheck-b", "r3-judge",
        ])
        self.assertEqual((reviewed.number, reviewed.previous), (3, 2))
        self.assertEqual(self.runner.problems, [])
        second = next(c["task"] for c in self.runner.calls if c["name"] == "r2-fix-1")
        self.assertIn(str(self.run.path / "rounds" / "2" / "final.md"), second)
        judge = next(c["task"] for c in self.runner.calls if c["name"] == "r3-judge")
        self.assertIn(f"--remerge-diff {self.head[:12]}..{reviewed.head[:12]}`", judge)  # every change made here
        self.assertIn("push", self.keys(reviewed))
        self.assertEqual([n for n in self.notes() if "ready" in n[0] or "reviewed" in n[0]], [
            ("placido · pr-7: the review is ready", "#7: fix-here; placido pr 7"),
            ("placido · pr-7: the fixes are reviewed", "#7: round 3, merge; placido pr 7"),
        ])  # rounds 2 and 3 waited for nobody

    def test_after_fix_rounds_the_menu_asks_again_and_nothing_goes_out(self):
        self.settings = self.load(CONFIG_WITH_ROUNDS.format(rounds=1))
        self.ctx = self.context()
        self.runner.findings_in = {2: {"A": [finding("A", 1)], "B": []}}
        reviewed = self.act("fix", self.review())
        self.assertEqual(reviewed.number, 2)
        self.assertNotIn("r2-fix-1", self.runner.names())
        listed = dict(prmenu.options(self.ctx, reviewed, prmenu.state(self.ctx, reviewed))[0])
        self.assertTrue(listed["fix"].startswith("Fix the 1 kept finding(s) here"), listed["fix"])
        self.assertNotIn("push", listed)
        with self.assertRaisesRegex(pr.PRStop, "nothing is pushed: round 2 kept F1, which are not minor"):
            self.act("push", reviewed)
        self.assertIn(("placido · pr-7: the fixes are reviewed",
                       "#7: round 2, fix-here; still fix-here after 1 fix(es): placido pr 7 fixes again"), self.notes())
        self.runner.findings_in = {}
        self.assertEqual(self.act("fix", reviewed).number, 3)

    def test_minor_findings_left_can_go_out_saying_so(self):
        self.settings = self.load(CONFIG_WITH_ROUNDS.format(rounds=1))
        self.ctx = self.context()
        self.runner.findings_in = {2: {"A": [finding("A", 1, severity="minor")], "B": []}}
        reviewed = self.act("fix", self.review())
        listed = dict(prmenu.options(self.ctx, reviewed, prmenu.state(self.ctx, reviewed))[0])
        self.assertEqual(listed["push"], "Push the 1 commit(s) made here, reviewed in full in round 2, leaving 1 minor"
                                         " finding(s), to alice/proj:greeting")
        self.act("push", reviewed)  # declined
        self.assertTrue(self.confirmed[-1].endswith("Round 2 leaves 1 minor finding(s) unfixed."))

    def test_a_round_that_closes_ends_the_fixes(self):
        self.runner.findings_in = {2: {"A": [finding("A", 1)], "B": []}}
        self.runner.during["r2-judge"] = lambda wt: setattr(self.runner, "final", {
            **self.runner.default_final(2), "verdict": "close"})
        reviewed = self.act("fix", self.review())
        self.assertEqual(reviewed.number, 2)
        self.assertNotIn("r2-fix-1", self.runner.names())
        self.assertEqual(self.keys(reviewed), ["edit", "edit-merge", "post", "ask", "close", "quit"])

    def test_a_fix_whose_review_stopped_is_reviewed_from_the_menu(self):
        rnd = self.review()
        self.runner.failures["r2-review-a"] = ["invalid"]
        with self.assertRaisesRegex(pr.PRStop, "reviewer A did not finish"):
            self.act("fix", rnd)
        listed = dict(prmenu.options(self.ctx, rnd, prmenu.state(self.ctx, rnd))[0])
        self.assertEqual(listed["review"], "Review the branch with the fix in full, in round 2: every reviewer, the"
                                           " cross-checks, and the judge")
        self.assertNotIn("push", listed)
        with self.assertRaisesRegex(pr.PRStop, "round 1's review was answered here already"):
            self.act("fix", rnd)
        reviewed = self.act("review", rnd)
        self.assertEqual((reviewed.number, self.runner.names().count("r2-review-a")), (2, 2))
        self.assertIn("push", self.keys(reviewed))

    def test_a_fix_that_changed_no_code_is_reviewed_in_full_too(self):
        self.runner.fix_change = False
        reviewed = self.fixed()
        self.assertEqual((reviewed.number, reviewed.head), (2, self.head))
        self.assertIn("merge", self.keys(reviewed))


class AskTest(MenuTestCase):
    """The agent the user can ask questions: a copy of the round's fixer's session, else
    the judge's, else a new agent; read-only, in a new tab of the run's workspace."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        home = mock.patch.dict("os.environ", {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)
        self.rnd = self.review()

    def session(self, step: str, role: str, agent: str, value: str, log: bool = True, resumed: bool = False) -> None:
        """A step's agent as placido records it, and its session's log where the agent keeps it."""

        self.run.event("step.start", step=step, role=role, **({"resumed": "x"} if resumed else {}))
        if not resumed:
            self.run.event("agent.start", step=step, agent=agent, model=f"{agent}-model", effort="high", pane="w2:p5")
        self.run.event("agent.session", step=step, agent=agent, kind="id", value=value)
        if log:
            path = (self.home / ".claude" / "projects" / "-wt" / f"{value}.jsonl" if agent == "claude"
                    else self.home / ".codex" / "sessions" / "2026" / "10" / "17" / f"rollout-2026-10-17T11-23-03-{value}.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")

    def opened(self) -> tuple[list[str], str]:
        """The agent's command and its tab's label, from what placido asked Herdr."""

        with redirect_stdout(io.StringIO()):
            self.act("ask", self.rnd)
        tab = next(c for c in self.fake.calls if c[1:3] == ["tab", "create"])
        run = next(c for c in self.fake.calls if c[1:3] == ["pane", "run"])
        self.assertEqual(run[3], "w2:p9")
        self.assertEqual(tab[tab.index("--cwd") + 1], str(self.worktree))
        self.assertIn(["herdr", "tab", "focus"], [c[:3] for c in self.fake.calls])
        return shlex.split(run[4]), tab[tab.index("--label") + 1]

    def test_a_copy_of_the_fixers_session(self):
        self.session("06-r1-judge", "judge", "claude", "judge-id")
        self.session("07-r1-fix-1", "fix", "claude", "fix-id")
        self.session("09-r1-fix-2", "fix", "claude", "fix-id", resumed=True)
        command, label = self.opened()
        self.assertEqual(label, "ask-fix")
        self.assertEqual(command[:9], ["claude", "--resume", "fix-id", "--fork-session", "--model", "claude-model",
                                       "--effort", "high", "--settings"])
        self.assertEqual(command[10:], ["--permission-mode", "plan", prmenu.ASK_COPY.format(number=7)])
        self.assertEqual(runlog.last_event(self.run.path, "pr.asked")["session"], "fix-id")

    def test_the_judges_when_the_fixers_log_is_gone(self):
        self.session("06-r1-judge", "judge", "codex", "judge-id")
        self.session("07-r1-fix-1", "fix", "claude", "fix-id", log=False)
        command, label = self.opened()
        self.assertEqual(label, "ask-judge")
        self.assertEqual(command[:4], ["codex", "fork", "judge-id", "-m"])
        self.assertEqual(command[-7:-1], ["-c", "features.memories=false", "-s", "read-only", "-a", "on-request"])

    def test_a_new_agent_when_no_session_of_the_round_can_be_copied(self):
        self.session("02-r1-fix-1", "fix", "claude", "old-round")
        self.rnd = replace(self.rnd, number=2)
        self.session("04-r2-scan", "pr-scan", "claude", "scan-id")
        listed, _ = prmenu.options(self.ctx, self.rnd, prmenu.state(self.ctx, self.rnd))
        self.assertIn("a new agent (claude, which knows only the review's files)", dict(listed)["ask"])
        command, label = self.opened()
        self.assertEqual(label, "ask")
        self.assertNotIn("--resume", command)
        self.assertEqual(command[-1], prmenu.ASK_NEW.format(number=7, folder=self.rnd.folder))
        self.assertIsNone(runlog.last_event(self.run.path, "pr.asked")["session"])


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
                         " contributor's branch, then review the branch with the fixes in full, in a new round; what"
                         " it keeps is fixed and reviewed in turn, up to 3 times. Committed locally, nothing pushed")
        reviewed = self.act("fix", rnd)
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
        since = (reviewed.folder / "since.patch").read_text()
        self.assertIn("Merge branch 'main' into greeting", since)
        self.assertIn("hello r1 fix-1", since)
        self.assertNotIn("later.txt", since)  # main's own changes were reviewed when they landed there
        self.assertEqual(reviewed.base, tip)  # what the squash merge lands
        self.assertNotIn("r1-merge", self.runner.names())
        listed = dict(prmenu.options(self.ctx, reviewed, prmenu.state(self.ctx, reviewed))[0])
        self.assertEqual(listed["push"], "Push the 2 commit(s) made here, reviewed in full in round 2, to"
                                         " alice/proj:greeting")
        self.assertIn(f"Merged main ({tip[:12]}) into the branch here as {merged['commit'][:12]}\n",
                      prmenu.summary(self.ctx, rnd))
        self.confirms = [True]
        self.act("push", reviewed)
        self.assertEqual(self.fork_head(), fix)

    def with_setup(self) -> Path:
        log = self.tmp / "setup.log"
        self.settings = self.load(CONFIG.replace('base = "main"', 'base = "main"\nsetup = "echo up >> $HOME_LOG"'))
        self.ctx = self.context()
        self.ctx.env = {"HOME_LOG": str(log)}
        return log

    def test_a_merge_that_changes_host_paths_sets_the_stack_up_again(self):
        log = self.with_setup()
        rnd = self.review()
        self.advance_main({".placido/notes/pr.md": "Node is in the container now.\n"})
        self.act("fix", rnd)
        self.assertEqual(runlog.last_event(self.run.path, "pr.base_merged")["host"], [".placido/notes/pr.md"])
        # For round 1, again after the merge, before the fix and its gates, then for round 2.
        self.assertEqual(log.read_text(), "up\nup\nup\n")
        r1 = [e["event"] for e in runlog.read_events(self.run.path) if e.get("round") == 1]
        self.assertEqual([k for k in r1 if k in ("pr.setup", "pr.base_merged", "pr.fixed")],
                         ["pr.setup", "pr.base_merged", "pr.setup", "pr.fixed"])

    def test_a_merge_that_leaves_the_host_paths_keeps_the_stack(self):
        log = self.with_setup()
        rnd = self.review()
        self.advance_main({"later.txt": "later\n"})
        self.act("fix", rnd)
        self.assertEqual(runlog.last_event(self.run.path, "pr.base_merged")["host"], [])
        self.assertEqual(log.read_text(), "up\nup\n")  # for round 1 and round 2 alone

    def test_a_merge_outlives_a_fix_that_did_not_finish(self):
        rnd = self.review()
        self.advance_main({"later.txt": "later\n"})
        self.runner.failures["r1-fix-1"] = ["invalid"]
        with self.assertRaisesRegex(pr.PRStop, "the fix did not finish"):
            self.act("fix", rnd)
        merge = runlog.last_event(self.run.path, "pr.base_merged")["commit"]
        self.assertNotIn("push", self.keys(rnd))
        with self.assertRaisesRegex(pr.PRStop, f"nothing is pushed: the worktree is at {merge[:12]}, not at"
                                               f" {self.head[:12]}, the head round 1 reviewed"):
            self.act("push", rnd)
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
        self.assertEqual([n for n in self.runner.names() if n.startswith(("r1-merge", "r1-fix"))],
                         ["r1-merge", "r1-fix-1"])
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
        fixer = next(c for c in self.runner.calls if c["name"] == "r1-fix-1")
        self.assertIn("placido did not merge it in, and you must not either", fixer["task"])
        self.assertIn("Nothing in the branch conflicts with main.", fixer["task"])

    def test_a_conflict_with_a_base_not_merged_in_is_named_to_the_fixer(self):
        self.push_new_head({".placido/stack.sh": "echo theirs\n"}, self.head)
        self.answers = [True]
        rnd = self.review()
        self.advance_main({".placido/stack.sh": "echo main's\n", "later.txt": "later\n"})
        self.act("fix", rnd)
        fixer = next(c for c in self.runner.calls if c["name"] == "r1-fix-1")
        self.assertIn("These files conflict with main: .placido/stack.sh. Where main already makes the branch's"
                      f" change to one, put it back as the branch's base ({rnd.base[:12]}) has it", fixer["task"])

    def test_a_branch_whose_fix_changed_host_paths_is_fixed_on_its_own_base(self):
        rnd = self.review()
        self.runner.during["r1-fix-1"] = lambda wt: (wt / ".placido" / "stack.sh").write_text("echo approved\n")
        self.runner.during["r2-judge"] = lambda wt: self.advance_main({"later.txt": "later\n"})
        self.runner.findings_in = {2: {"A": [finding("A", 1)], "B": []}}
        self.answers = [True]
        reviewed = self.act("fix", rnd)
        first = pr.round_by_number(self.run.path, 2).head
        skipped = runlog.last_event(self.run.path, "pr.base_merge_skipped")
        self.assertEqual((skipped["round"], skipped["reason"]), (2, "host paths"))
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD^"), first)
        self.assertEqual((reviewed.number, len(self.asked)), (3, 1))  # each later round trusts the approved change

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
        rnd = self.fixed()
        fix = self.local_head()
        s = prmenu.state(self.ctx, rnd)
        self.assertEqual(prmenu._last_text(self.ctx, s), " · last gates not run")
        listed, _ = prmenu.options(self.ctx, rnd, s)
        self.assertEqual([k for k, _ in listed], ["edit", "edit-merge", "last", "push", "post", "ask", "close", "quit"])
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
        rnd = self.fixed()
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
        self.runner.final = None  # round 2's judge keeps nothing
        self.act("fix", rnd)
        listed = json.loads((rnd.folder / "fix-findings-1.json").read_text())["findings"]
        self.assertEqual(listed[0]["fix"], "Narrow the doc comment to a later statement.")
        for name in ("r1-fix-1", "r2-review-a", "r2-review-b", "r2-judge"):
            task = next(c["task"] for c in self.runner.calls if c["name"] == name)
            self.assertIn(f"{decisions.path(self.run.path)}. Read them; where one differs from the review, the"
                          " decision wins.", task)

    def test_a_decision_can_drop_a_finding_add_work_and_change_the_verdict(self):
        added = {"title": "close in the step helpers", "area": "gaps", "severity": "significant", "size": "medium",
                 "location": "steps.go:9", "description": "each step keeps its connection", "fix": "close it first",
                 "id": "X1"}
        self.runner.decided = {"answers": [answer(1, drop=["F1"], add=[added]), answer(2, as_recommended=True)],
                               "verdict": "close"}
        self.runner.drafts = {"comment.md": "Thanks.\n\nThis is not the way to fix it.\n"}
        rnd = self.review(decisions=TWO)
        final = self.final(rnd)
        self.assertEqual((final["verdict"], final["judge_verdict"]), ("close", "fix-here"))
        f1, f2 = final["findings"]
        self.assertEqual((f1["decision"], f1["reason"]), ("drop", "Your decision 1: narrow it"))
        self.assertEqual((f2["id"], f2["sources"], f2["agreement"], f2["decision"], f2["fix"], f2["decided_by"]),
                         ("F2", ["decision 1"], "yours", "keep", "close it first", 1))
        decided = runlog.last_event(self.run.path, "pr.decided")
        self.assertEqual((decided["verdict"], decided["changed"], decided["dropped"], decided["added"]),
                         ("close", [], ["F1"], ["F2"]))
        self.assertFalse((rnd.folder / "merge.md").exists())
        self.assertIn("# Verdict: close (yours; the judge's was fix-here)", (rnd.folder / "final.md").read_text())
        text = prmenu.summary(self.ctx, rnd)
        self.assertIn("Verdict: close (yours; the judge's was fix-here). It greets", text)
        self.assertIn("F2  significant gaps        `steps.go:9` close in the step helpers [decision 1: yours]\n", text)
        self.assertIn("F1  the greeting [A1, B1]: Your decision 1: narrow it", text)
        self.assertEqual(self.keys(rnd), ["edit", "post", "ask", "close", "quit"])
        self.confirms = [True]
        self.act("post", rnd)
        self.assertEqual(self.hub.calls[-1][:2], ("review", "comment"))

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
        self.assertEqual(problems(verdict="ship"), ["verdict 'ship': use merge, fix-here, close."])
        self.assertEqual(problems(verdict="request-changes"), ["verdict 'request-changes': use merge, fix-here, close."])
        self.assertEqual(problems(verdict="merge"), ['a "merge" verdict fixes nothing, but F1 are still kept: drop'
                                                     ' them, or keep the verdict that fixes them.'])
        self.assertEqual(problems(a1={"drop": ["F1"]}),
                         ['no finding is left to fix, so the verdict is "merge" or "close", not "fix-here".'])
        self.assertEqual(problems(a1={"drop": ["F1"]}, verdict="merge"), [])
        self.assertEqual(problems(a1={"drop": ["F1"], "add": [
            {"title": "t", "area": "gaps", "severity": "minor", "size": "small", "description": "d", "fix": "f"}]}), [])
        (folder / "merge.md").write_text("A" * 73 + "\n")
        self.assertEqual(problems(), ["the squash subject is 73 characters; keep it to 72."])
        self.assertEqual(problems(verdict="close"), [])  # nothing is merged, so no squash message
        (folder / "comment.md").write_text(" \n")
        self.assertEqual(problems(verdict="close"), [f"{folder / 'comment.md'} is missing or empty."])
