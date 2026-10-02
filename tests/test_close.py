import os
from pathlib import Path

from placido import close, config, runlog, start
from placido.herdr import Herdr
from placido.proc import Result
from tests.test_start import StartTestCase, make_repo, sh


class CloseTest(StartTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.tmp, setup="true")
        self.run = self.start("02", self.repo)
        self.worktree = self.tmp / "worktrees" / "placido-02-many-names"

    def settings(self, teardown: str = "") -> config.Config:
        cfg = self.tmp / "close.toml"
        cfg.write_text(f'[project]\nteardown = "{teardown}"\n')
        return config.load(cfg)

    def close(self, teardown: str = "", force: bool = False, here: str | None = None) -> None:
        close.close(self.run, Herdr(self.fake), self.settings(teardown), force, here)

    def test_tears_down_removes_the_worktree_and_ends_the_run(self):
        self.close(teardown="echo $PLACIDO_NAME > $PLACIDO_RUN_DIR/torn-down")
        self.assertFalse(self.worktree.exists())
        # The branch has no commits of its own, so it counts as merged and goes.
        self.assertNotIn("placido/02-many-names", sh(self.repo, "git", "branch"))
        self.assertEqual((self.run.path / "torn-down").read_text(), "proj-02-many-names\n")
        events = self.events(self.run.path)
        self.assertLess(events.index("teardown.end"), events.index("worktree.removed"))
        self.assertEqual(runlog.last_event(self.run.path, "run.end")["outcome"], "abandoned")
        self.assertIn("run.closed", events)
        self.assertTrue((self.run.path / "summary.md").is_file())
        self.assertEqual(runlog.active_runs(self.runs, "proj", str(self.repo)), [])

    def test_a_finished_run_keeps_its_outcome(self):
        self.run.event("run.end", outcome="passed")
        self.close()
        ends = [e for e in runlog.read_events(self.run.path) if e["event"] == "run.end"]
        self.assertEqual([e["outcome"] for e in ends], ["passed"])

    def test_uncommitted_changes_are_refused(self):
        (self.worktree / "wip.txt").write_text("half done\n")
        with self.assertRaisesRegex(close.CloseError, r"uncommitted changes:\n\?\? wip.txt"):
            self.close(teardown="touch $PLACIDO_RUN_DIR/torn-down")
        self.assertTrue(self.worktree.exists())
        self.assertFalse((self.run.path / "torn-down").exists())  # nothing was torn down
        self.assertNotIn("run.closed", self.events(self.run.path))

    def test_force_discards_them(self):
        (self.worktree / "wip.txt").write_text("half done\n")
        self.close(force=True)
        self.assertFalse(self.worktree.exists())
        self.assertTrue(runlog.last_event(self.run.path, "worktree.removed")["forced"])

    def test_a_failing_teardown_stops_unless_forced(self):
        with self.assertRaisesRegex(close.CloseError, "teardown failed with exit 3"):
            self.close(teardown="exit 3")
        self.assertTrue(self.worktree.exists())
        self.close(teardown="exit 3", force=True)
        self.assertFalse(self.worktree.exists())

    def test_refuses_from_a_pane_in_the_issue_workspace(self):
        with self.assertRaisesRegex(close.CloseError, "belongs to the issue's workspace"):
            self.close(here="w2:p4")
        self.close(here="w1:p1")
        self.assertFalse(self.worktree.exists())

    def test_refuses_while_placido_works_on_the_run(self):
        (self.run.path / "lock").write_text(f"{os.getpid()}\n")
        with self.assertRaisesRegex(close.CloseError, "already working on this run"):
            self.close()

    def test_falls_back_to_git_when_herdr_lost_the_workspace(self):
        self.fake.fail["worktree remove"] = Result(1, '{"error":{"code":"workspace_not_found","message":"no w2"}}')
        self.close()
        self.assertFalse(self.worktree.exists())
        self.assertIsNone(runlog.last_event(self.run.path, "worktree.removed")["workspace"])

    def test_a_worktree_already_gone_still_closes(self):
        sh(self.repo, "git", "worktree", "remove", str(self.worktree))
        self.close()
        self.assertIn("worktree.missing", self.events(self.run.path))
        self.assertIn("run.closed", self.events(self.run.path))


class TidyTest(StartTestCase):
    """After the worktree goes: the base is brought up to date in the main checkout,
    and the branch is deleted once its work is merged (decided 2026-10-02)."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.tmp, setup="true")
        self.run = self.start("02", self.repo)
        self.worktree = self.tmp / "worktrees" / "placido-02-many-names"
        self.gh_calls: list[list[str]] = []
        self.pr_state = "OPEN"

    def gh(self, argv, timeout=None):
        self.gh_calls.append(argv)
        return Result(0, self.pr_state + "\n")

    def close(self) -> None:
        cfg = self.tmp / "close.toml"
        cfg.write_text("[project]\n")
        close.close(self.run, Herdr(self.fake), config.load(cfg), gh=self.gh)

    def commit_in_worktree(self) -> None:
        (self.worktree / "new.txt").write_text("x\n")
        sh(self.worktree, "git", "add", "-A")
        sh(self.worktree, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "work")

    def branches(self) -> str:
        return sh(self.repo, "git", "branch", "--list", "placido/*")

    def test_a_branch_merged_into_the_base_is_deleted(self):
        self.commit_in_worktree()
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-edit",
           "placido/02-many-names")
        self.close()
        self.assertEqual(self.branches(), "")
        self.assertEqual(runlog.last_event(self.run.path, "branch.deleted")["reason"], "merged into main")

    def test_an_unmerged_branch_stays(self):
        self.commit_in_worktree()
        self.close()
        self.assertIn("placido/02-many-names", self.branches())
        self.assertEqual(runlog.last_event(self.run.path, "branch.kept")["reason"], "not merged")

    def test_a_squash_merged_pull_request_deletes_the_branch(self):
        self.commit_in_worktree()  # squashed on GitHub, so git cannot see the merge
        sh(self.repo, "git", "remote", "add", "origin", "https://github.com/leodip/proj.git")
        self.run.event("pr.opened", url="https://github.com/leodip/proj/pull/460", number=460)
        self.pr_state = "MERGED"
        self.close()
        self.assertEqual(self.branches(), "")
        self.assertEqual(runlog.last_event(self.run.path, "branch.deleted")["reason"], "pull request #460 merged")
        self.assertEqual(self.gh_calls[0][:4], ["gh", "pr", "view", "460"])

    def test_an_open_pull_request_keeps_the_branch(self):
        self.commit_in_worktree()
        sh(self.repo, "git", "remote", "add", "origin", "https://github.com/leodip/proj.git")
        self.run.event("pr.opened", url="https://github.com/leodip/proj/pull/460", number=460)
        self.close()
        self.assertIn("placido/02-many-names", self.branches())

    def test_the_base_is_pulled_as_a_fast_forward(self):
        origin = self.tmp / "origin.git"
        sh(self.tmp, "git", "clone", "-q", "--bare", str(self.repo), str(origin))
        sh(self.repo, "git", "remote", "add", "origin", str(origin))
        sh(self.repo, "git", "fetch", "-q", "origin")
        sh(self.repo, "git", "branch", "-q", "--set-upstream-to=origin/main", "main")
        other = self.tmp / "other"
        sh(self.tmp, "git", "clone", "-q", str(origin), str(other))
        (other / "merged.txt").write_text("the merged change\n")
        sh(other, "git", "add", "-A")
        sh(other, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "merged")
        sh(other, "git", "push", "-q", "origin", "main")
        self.close()
        self.assertTrue((self.repo / "merged.txt").is_file())
        self.assertIn("base.pulled", self.events(self.run.path))

    def test_no_pull_with_uncommitted_changes_or_on_another_branch(self):
        sh(self.repo, "git", "remote", "add", "origin", str(self.tmp / "nowhere.git"))
        (self.repo / "README").write_text("edited\n")
        sh(self.repo, "git", "add", "README")
        self.close()
        skipped = runlog.last_event(self.run.path, "base.pull_skipped")
        self.assertEqual(skipped["reason"], "the checkout has uncommitted changes")


class FindRunTest(StartTestCase):
    def test_by_prefix(self):
        repo = make_repo(self.tmp, setup="true")
        first = self.start("01", repo)
        second = self.start("02", repo)
        self.assertEqual(start.find_run(repo, self.runs, "02"), second.path)
        self.assertEqual(start.find_run(repo, self.runs, "01-shout-flag"), first.path)
        with self.assertRaisesRegex(start.StartError, "ambiguous: 01-shout-flag, 02-many-names"):
            start.find_run(repo, self.runs, "0")
        with self.assertRaisesRegex(start.StartError, "no active run of proj matches '9'"):
            start.find_run(repo, self.runs, "9")
