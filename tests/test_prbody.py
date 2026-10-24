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

    def test_the_agreement_s_release_notes_are_a_comment_the_body_links(self):
        self.assertEqual(prbody.release_notes(self.run.path), "")
        notes = "- Copy the console's templates again.\n- Durations read \"1 hour, 30 minutes\"."
        (self.run.path.parent / "agreement.md").write_text(
            AGREEMENT.replace("## Decisions", f"## Release notes\n{notes}\n\n## Decisions"))
        self.assertEqual(prbody.release_notes(self.run.path), f"## Release notes\n\n{notes}\n")
        self.assertNotIn("Release notes", prbody.build(self.run.path, []))
        self.run.event("release_notes.posted", url="https://x/pull/7#issuecomment-1", on="pr 7")
        self.assertIn("stops the server.\n\n## Release notes\n\nWhat upgrading asks of the people who run the"
                      " project: [the release notes comment](https://x/pull/7#issuecomment-1).\n\n## Decisions\n",
                      prbody.build(self.run.path, []))

    def test_the_benchmark_and_what_each_slice_measured(self):
        self.assertNotIn("## Benchmark", prbody.build(self.run.path, []))
        benchmark = "Wall time, in minutes.\n- **Command:** e2e\n- **Baseline:** 14.7\n- **Target:** 8"
        (self.run.path.parent / "agreement.md").write_text(
            AGREEMENT.replace("## Decisions", f"## Benchmark\n{benchmark}\n\n## Decisions"))
        for n, evidence in ((1, "- **Benchmark:** 14.7 → 11.2 min\n"), (2, "- **Green:** passed\n")):
            self.run.event("slice.committed", slice=n)
            (self.run.step_dir(n + 2, f"implement-slice-{n}") / "result.md").write_text(
                f"Speed it up\n\n## Evidence\n{evidence}")
        body = prbody.build(self.run.path, [])
        self.assertIn(f"stops the server.\n\n## Benchmark\n\n{benchmark}\n\nMeasured by the slices, before → after:"
                      "\n\n- Slice 1: 14.7 → 11.2 min\n\n## Decisions", body)

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

    def test_last_gates_that_fail_are_said_first(self):
        self.run.event("last_gates.gave_up", gates=["slow"])
        body = prbody.build(self.run.path, [])
        self.assertTrue(body.startswith("Closes #331\n\n> **The last gates fail after placido's fixes (slow).**"))

    def test_last_gates_failing_from_outside_the_change_say_why(self):
        self.run.event("checks.unchanged", what="the last gates", why="main fails them too.")
        self.run.event("last_gates.gave_up", gates=["slow"])
        body = prbody.build(self.run.path, [])
        self.assertTrue(body.startswith("Closes #331\n\n> **The last gates fail after placido's fixes (slow).**"
                                        " Nothing in this change causes it: main fails them too.\n"))

    def test_no_review_yet(self):
        body = prbody.build(self.run.path, [])
        self.assertNotIn("## Review", body)
