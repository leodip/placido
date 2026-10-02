import tempfile
import time
import unittest
from pathlib import Path

from placido import runlog, status


class StatusTest(unittest.TestCase):
    def test_text_starts_with_its_state(self):
        self.assertEqual(status.text(status.YOU, "answer in implement-slice-2"), "you · answer in implement-slice-2")
        self.assertEqual(status.text(status.DONE), "done")

    def test_label_drops_the_step_number(self):
        self.assertEqual(status.label("02-implement-slice-1"), "implement-slice-1")
        self.assertEqual(status.label("spec"), "spec")

    def test_kept_in_the_run_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            status.show(Path(tmp), "working · slice 1/2")
            self.assertEqual(status.read(Path(tmp)), "working · slice 1/2")
            self.assertEqual(status.read(Path(tmp) / "missing"), "")


class BoardTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "runs"
        self.trees = Path(tmp.name) / "trees"

    def run_of(self, project: str, issue: str, shown: str | None, *events: str) -> runlog.Run:
        run = runlog.Run.create(self.root, project, issue, {})
        tree = self.trees / issue
        tree.mkdir(parents=True)
        run.event("worktree.created", path=str(tree), workspace="w2")
        for kind in events:
            run.event(kind, outcome="passed")
        if shown is not None:
            status.show(run.path, shown)
        return run

    def test_issues_that_need_you_come_first(self):
        self.run_of("goiabada", "439-rate-limit", "working · slice 2/3")
        self.run_of("sandbox", "04-farewell", "you · answer in implement-slice-1")
        self.run_of("sandbox", "02-many-names", None, "run.end")
        closed = self.run_of("sandbox", "01-shout", "done · review passed", "run.closed")
        lines = status.board(self.root, now=time.time()).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("sandbox   04-farewell     you · answer in implement-slice-1  ("))
        self.assertIn("goiabada  439-rate-limit  working · slice 2/3", lines[1])
        self.assertIn("sandbox   02-many-names   done · passed", lines[2])
        self.assertNotIn(closed.path.parent.name, "\n".join(lines))

    def test_nothing_in_progress(self):
        self.assertEqual(status.board(self.root), "No issues in progress.\n")
