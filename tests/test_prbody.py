import tempfile
import unittest
from pathlib import Path

from placido import prbody, review, runlog

AGREEMENT = """# Agreement: strip release binaries

## Problem
Too big.

## Solution
Every shipped binary is linked with `-w`.

A `TZ` that names no zone stops the server.

## Decisions
1. **The release builds drop the DWARF data (`-w`) and keep the symbol
   table: no `-s`.** Measured on go1.27.
2. **A `TZ` naming no zone stops the server.** Rejected: UTC.

## Seams
- the process boundary
"""


class BodyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = runlog.Run.create(Path(tmp.name) / "runs", "goiabada", "331-strip",
                                     {"issue_number": 331, "issue_title": "strip"})
        (self.run.path.parent / "agreement.md").write_text(AGREEMENT)
        self.run.event("step.start", step="04-implement-slice-2")

    def review(self, outcome: str = "passed") -> None:
        account = review.Review(outcome=outcome, rounds=[
            {"round": 1, "verdicts": [], "commit": "2d858ee240", "answers": {"R1-1": "fixed"},
             "findings": [{"id": "R1-1", "severity": "blocking", "axis": "spec", "location": "check.yml:728",
                           "title": "The smoke script is not executable"}]},
            {"round": 2, "verdicts": [{"id": "R1-1", "verdict": "resolved"}], "findings": []},
        ], followups=[{"id": "F1", "title": "Replace chi", "why": "decision 9"}])
        if outcome == "escalated":
            account.escalated = [{"id": "R1-2", "severity": "blocking", "title": "Windows TZ", "why": "your call"}]
        review.summary(account, self.run.path)

    def test_written_for_the_reviewer(self):
        self.review()
        body = prbody.build(self.run.path, [331, 340])
        self.assertTrue(body.startswith("Closes #331\nCloses #340\n\n## What this changes\n\nEvery shipped binary"))
        self.assertIn("A `TZ` that names no zone stops the server.\n\n## Decisions", body)
        self.assertIn("1. The release builds drop the DWARF data (`-w`) and keep the symbol table: no `-s`.\n"
                      "2. A `TZ` naming no zone stops the server.\n", body)
        self.assertIn("## Review\n\nPassed in 2 rounds.\n\n- **blocking** The smoke script is not executable:"
                      " fixed in 2d858ee → resolved (round 2)", body)
        self.assertNotIn("Replace chi", body)  # follow-ups live in their own comment
        self.assertTrue(body.endswith(f"Built by placido · run `331-strip/{self.run.path.name}`\n"))

    def test_findings_left_for_the_user_are_kept(self):
        self.review("escalated")
        body = prbody.build(self.run.path, [])
        self.assertIn("Ended after 2 rounds with findings that need your decision, below.", body)
        self.assertIn("### Needs your decision\n\n- **R1-2** (blocking) Windows TZ: your call", body)

    def test_decisions_made_during_the_run(self):
        (self.run.path.parent / "decisions.md").write_text(
            "# Decisions\n\n## D1 · 04-implement-slice-2 · 2026-10-02\n**Question:** Wording?\n"
            "**Decision:** \"See you\".\n**Why:** shorter.\n\n## D2 · 09-other-run · 2026-10-01\n"
            "**Decision:** not this run's.\n"
        )
        body = prbody.build(self.run.path, [])
        self.assertIn("Decided during the run:\n\n- D1: \"See you\".", body)
        self.assertNotIn("not this run's", body)

    def test_ci_that_is_not_green_is_said_first(self):
        self.run.event("ci.result", outcome="red", attempt=3, failed=["mysql"])
        body = prbody.build(self.run.path, [])
        self.assertTrue(body.startswith("Closes #331\n\n> **CI is red after placido's fixes (mysql).**"))

    def test_no_review_yet(self):
        body = prbody.build(self.run.path, [])
        self.assertNotIn("## Review", body)
