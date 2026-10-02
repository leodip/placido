import tempfile
import unittest
from pathlib import Path

from placido import decisions, runlog

ENTRY = "## D{n} · {step} · 2026-10-02\n**Question:** q\n**Decision:** d\n**Why:** w\n\n"


class DecisionsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = runlog.Run.create(Path(tmp.name), "proj", "02-x", {})
        self.folder = self.run.step_dir(3, "implement-slice-2")

    def record(self, n: int, step: str) -> None:
        with decisions.path(self.run.path).open("a") as out:
            out.write(ENTRY.format(n=n, step=step))

    def test_lives_beside_the_agreement_for_every_run(self):
        self.assertEqual(decisions.path(self.run.path), self.run.path.parent / "decisions.md")

    def test_a_step_that_asked_nothing_needs_nothing(self):
        self.assertEqual(decisions.check(self.folder, self.run.path), [])

    def test_each_question_needs_its_decision(self):
        (self.folder / "question-1.md").write_text("Blocked: which?\n")
        (self.folder / "question-2.md").write_text("Blocked: and?\n")
        self.record(1, "01-implement-slice-1")  # another step's decision does not count
        self.record(2, self.folder.name)
        (problem,) = decisions.check(self.folder, self.run.path)
        self.assertIn("2 questions but recorded 1 decision", problem)
        self.assertIn(f"'## D3 · {self.folder.name} · ", problem)
        self.record(3, self.folder.name)
        self.assertEqual(decisions.check(self.folder, self.run.path), [])

    def test_prompt_line(self):
        self.assertIn("none yet", decisions.prompt_line(self.run.path))
        self.record(1, "03-implement-slice-2")
        self.assertIn("the decision wins", decisions.prompt_line(self.run.path))
