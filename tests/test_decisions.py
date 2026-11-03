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

    def gates(self, n: int, step: str, line: str) -> None:
        with decisions.path(self.run.path).open("a") as out:
            out.write(ENTRY.format(n=n, step=step).rstrip("\n") + f"\n**Gates:** {line}\n\n")

    def test_no_decision_sets_gates(self):
        self.record(1, "03-implement-slice-2")
        self.assertIsNone(decisions.gates(self.run.path, 2))

    def test_the_latest_decision_about_the_slice_sets_its_gates(self):
        # #718: slice 4's sealed race gate could not run, and the user chose the race tier.
        self.gates(1, "05-implement-slice-4", "`unit --race`")
        self.gates(2, "06-implement-slice-12", "`lint`")  # slice 12, not slice 2 or 4
        self.record(3, "07-implement-slice-4")  # a decision without gates changes nothing
        self.assertEqual(decisions.gates(self.run.path, 4), decisions.Gates(1, ["unit --race"]))
        self.gates(4, "08-implement-slice-4", "`exec 'go test ./x/...'`, `lint`.")
        self.assertEqual(decisions.gates(self.run.path, 4), decisions.Gates(4, ["exec 'go test ./x/...'", "lint"]))
        self.assertIsNone(decisions.gates(self.run.path, 2))

    def test_none_leaves_only_the_project_gates(self):
        self.gates(1, "05-implement-slice-4", "none.")
        self.assertEqual(decisions.gates(self.run.path, 4), decisions.Gates(1, []))

    def test_a_gate_without_backticks_is_a_problem(self):
        self.gates(1, "05-implement-slice-4", "unit --race")
        found = decisions.gates(self.run.path, 4)
        self.assertIn("D1's **Gates:** line names no gate", found.problem)

    def test_prompt_line(self):
        self.assertIn("none yet", decisions.prompt_line(self.run.path))
        self.record(1, "03-implement-slice-2")
        self.assertIn("the decision wins", decisions.prompt_line(self.run.path))
