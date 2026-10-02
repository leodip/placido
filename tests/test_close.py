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
        self.assertIn("placido/02-many-names", sh(self.repo, "git", "branch"))  # the branch stays
        self.assertEqual((self.run.path / "torn-down").read_text(), "proj-02-many-names\n")
        events = self.events(self.run.path)
        self.assertLess(events.index("teardown.end"), events.index("worktree.removed"))
        self.assertEqual(runlog.last_event(self.run.path, "run.end")["outcome"], "abandoned")
        self.assertEqual(events[-1], "run.closed")
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
        self.assertEqual(self.events(self.run.path)[-1], "run.closed")


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
