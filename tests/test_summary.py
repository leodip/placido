import json
import tempfile
import unittest
from pathlib import Path

from placido import runlog, summary

SLICES = {"slices": [
    {"id": 1, "title": "Option parsing", "delivers": "a", "blocked_by": [], "gates": []},
    {"id": 2, "title": "Languages", "delivers": "b", "blocked_by": [1], "gates": []},
]}
RESULT = (
    "Build slice 1\n\nWhat.\n\n## Evidence\n- ok\n\n## Follow-ups\n- Cache the table.\n"
    "- Nothing mentions `--bye`. The agreement chose\n  this (decision 2).\n- None\n"
)


class SummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = runlog.Run.create(Path(tmp.name), "proj", "03-x", {
            "issue_title": "Greet in three languages", "issue_source": "owner/repo#3",
            "branch": "placido/03-x", "base": "main", "base_commit": "646c2ffd4c5d",
        })
        (self.run.path.parent / "slices.json").write_text(json.dumps(SLICES))

    def test_a_run_in_progress(self):
        self.run.event("run.start")
        text = summary.write(self.run.path)
        self.assertTrue(text.startswith("# Greet in three languages\n"))
        self.assertIn("- **State:** in progress", text)
        self.assertIn("- **Issue:** owner/repo#3", text)
        self.assertIn("- **Branch:** `placido/03-x`, from `main` at 646c2ff", text)
        self.assertIn("- 1. Option parsing: not built", text)
        self.assertIn("## Review\n\nNot run yet.", text)
        self.assertEqual((self.run.path / "summary.md").read_text(), text)

    def test_a_stopped_run_says_how_to_resume(self):
        self.run.event("run.start")
        text = summary.write(self.run.path, stopped="gave-up")
        self.assertIn("- **State:** stopped (gave-up); run placido again to resume", text)

    def test_a_finished_run(self):
        self.run.event("run.start")
        self.run.event("step.start", step="01-implement-slice-1")
        self.run.event("slice.failed", slice=1, outcome="invalid")
        self.run.event("step.start", step="02-implement-slice-1")
        self.run.event("slice.committed", slice=1, commit="abcdef1234")
        self.run.event("agent.fallback", role="implement", reason="refusal", message="flagged",
                       **{"from": "claude opus high", "to": "codex gpt-daybreak-blue-latest max"})
        self.run.event("slice.committed", slice=2, commit="1234567abc")
        self.run.event("run.end", outcome="passed")
        step = self.run.step_dir(2, "implement-slice-1")
        (step / "result.md").write_text(RESULT)
        (self.run.path / "review.md").write_text("# Review: passed\n\n## Rounds\n\n- Round 1: no new findings.\n")
        (self.run.path.parent / "decisions.md").write_text(
            "## D1 · 09-implement-slice-1 · 2026-10-01\n**Question:** old run\n**Decision:** x\n**Why:** y\n\n"
            "## D2 · 02-implement-slice-1 · 2026-10-02\n**Question:** Which store?\n"
            "**Decision:** Redis.\n**Why:** It is authoritative.\n"
        )
        text = summary.write(self.run.path)
        self.assertIn("- **State:** passed", text)
        self.assertIn("- 1. Option parsing: `abcdef1` (after 1 failed attempt: invalid)", text)
        self.assertIn("- 2. Languages: `1234567`", text)
        self.assertIn("## Decisions you made\n\n- **D2 · 02-implement-slice-1 · 2026-10-02**\n"
                      "  **Question:** Which store?\n  **Decision:** Redis.", text)
        self.assertNotIn("old run", text)  # another run's decision
        self.assertIn("## Review: passed\n\n### Rounds", text)
        self.assertIn(
            "## Follow-ups from the slices\n\n- Slice 1: Cache the table.\n"
            "- Slice 1: Nothing mentions `--bye`. The agreement chose this (decision 2).\n\n", text,
        )
        self.assertIn("- implement moved from claude opus high to codex gpt-daybreak-blue-latest max"
                      " after a refusal: flagged", text)


if __name__ == "__main__":
    unittest.main()
