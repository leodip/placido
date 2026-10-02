import json
import tempfile
import unittest
from pathlib import Path

from placido import report, runlog


class ReportTest(unittest.TestCase):
    """The report must match the event log it is read from."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.run = runlog.Run.create(self.tmp / "runs", "proj", "04-bye", {"issue_title": "Say goodbye"})
        (self.run.path.parent / "slices.json").write_text(json.dumps({"slices": [{"id": 1}, {"id": 2}]}))
        worktree = self.tmp / "wt"
        worktree.mkdir()
        self.log(
            ("2026-10-02T14:50:00-03:00", "run.start", {}),
            ("2026-10-02T14:50:00-03:00", "worktree.created", {"path": str(worktree)}),
            ("2026-10-02T14:55:00-03:00", "slice.start", {"slice": 1}),
            ("2026-10-02T14:55:00-03:00", "step.start", {"step": "02-implement-slice-1", "role": "implement"}),
            ("2026-10-02T14:55:00-03:00", "agent.start", {"step": "02-implement-slice-1", "agent": "claude",
                                                          "model": "opus", "effort": "high", "name": "impl"}),
            ("2026-10-02T14:56:00-03:00", "agent.question", {"step": "02-implement-slice-1", "question": "Which?"}),
            ("2026-10-02T14:58:30-03:00", "agent.answered", {"step": "02-implement-slice-1"}),
            ("2026-10-02T14:59:00-03:00", "result.rejected", {"step": "02-implement-slice-1", "problems": ["x"]}),
            ("2026-10-02T15:00:00-03:00", "step.end", {"step": "02-implement-slice-1", "outcome": "success",
                                                       "seconds": 300}),
            ("2026-10-02T15:00:00-03:00", "slice.committed", {"slice": 1}),
            ("2026-10-02T15:00:00-03:00", "review.round", {"round": 1}),
            ("2026-10-02T15:00:00-03:00", "step.start", {"step": "03-review-1", "role": "review"}),
            ("2026-10-02T15:00:00-03:00", "agent.start", {"step": "03-review-1", "agent": "codex",
                                                          "model": "gpt-6.1-sol", "effort": "max", "name": "rev"}),
            ("2026-10-02T15:02:00-03:00", "step.end", {"step": "03-review-1", "outcome": "success", "seconds": 120}),
            ("2026-10-02T15:02:00-03:00", "review.findings", {"round": 1, "blocking": 1, "significant": 0, "minor": 2}),
            ("2026-10-02T15:03:00-03:00", "review.round", {"round": 2}),
            ("2026-10-02T15:03:00-03:00", "step.start", {"step": "05-review-2", "role": "review", "resumed": "rev"}),
            ("2026-10-02T15:04:00-03:00", "step.end", {"step": "05-review-2", "outcome": "success", "seconds": 60}),
            ("2026-10-02T15:04:00-03:00", "agent.fallback", {"role": "review", "from": "codex a", "to": "pi b",
                                                             "reason": "refusal"}),
            ("2026-10-02T15:04:00-03:00", "agent.failed", {"step": "05-review-2", "kind": "refusal"}),
            ("2026-10-02T15:05:00-03:00", "review.done", {"outcome": "passed"}),
            ("2026-10-02T15:05:00-03:00", "run.end", {"outcome": "passed"}),
        )
        step = self.run.path / "steps" / "02-implement-slice-1"
        step.mkdir(parents=True)
        (step / "transcript.jsonl").write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-10-02T17:57:00Z",
            "message": {"id": "a", "usage": {"input_tokens": 10, "cache_read_input_tokens": 990, "output_tokens": 5}},
        }) + "\n")

    def log(self, *entries) -> None:
        with (self.run.path / "events.jsonl").open("w") as out:
            for ts, kind, fields in entries:
                out.write(json.dumps({"ts": ts, "event": kind, **fields}) + "\n")

    def test_one_run(self):
        built = report.build(self.run.path)
        self.assertEqual((built.state, built.closed, built.seconds), ("passed", False, 15 * 60))
        self.assertEqual([s.name for s in built.steps], ["02-implement-slice-1", "03-review-1", "05-review-2"])
        first = built.steps[0]
        self.assertEqual((first.agent, first.outcome, first.seconds), ("claude opus high", "success", 300))
        self.assertEqual(first.notes, ["asked you", "result sent back"])
        self.assertEqual(first.usage.total, 1005)
        self.assertEqual(built.steps[2].agent, "codex gpt-6.1-sol max")  # the resumed reviewer
        self.assertEqual(built.steps[2].notes, ["resumed", "refusal"])
        self.assertEqual((built.slices, built.committed, built.attempts), (2, 1, 1))
        self.assertEqual((built.rounds, built.review), (2, "passed"))
        self.assertEqual(built.findings, {"blocking": 1, "significant": 0, "minor": 2})
        self.assertEqual((built.questions, built.waited, built.refusals), (1, 150, 1))
        self.assertEqual(built.fallbacks, ["review: codex a → pi b (refusal)"])

        text = report.render(built)
        self.assertIn("04-bye · Say goodbye\npassed · started 2026-10-02 ", text)
        self.assertIn("02-implement-slice-1  claude opus high", text)
        self.assertIn("5m 00s  success   1.0k tok (99% cached)  asked you, result sent back", text)
        self.assertIn("Slices   1 of 2 committed", text)
        self.assertIn("Review   passed in 2 rounds; findings: 1 blocking, 0 significant, 2 minor", text)
        self.assertIn("You      1 question; 2m 30s waiting for you", text)
        self.assertIn("Usage    claude 1.0k tokens (99% cached, 5 out)", text)
        self.assertIn("Trouble  review: codex a → pi b (refusal); 1 refusal(s)", text)

    def test_a_run_whose_worktree_vanished(self):
        self.log(("2026-10-02T14:50:00-03:00", "run.start", {}),
                 ("2026-10-02T14:50:00-03:00", "worktree.created", {"path": str(self.tmp / "gone")}))
        self.assertEqual(report.build(self.run.path).state, "left (worktree gone)")

    def test_json_matches(self):
        data = report.as_dict(report.build(self.run.path))
        self.assertEqual(data["review"], {"outcome": "passed", "rounds": 2, "findings":
                                          {"blocking": 1, "significant": 0, "minor": 2}})
        self.assertEqual(data["steps"][0]["usage"]["total"], 1005)
        json.dumps(data)  # serializable

    def test_trends(self):
        text = report.trends([report.build(self.run.path)])
        self.assertIn("04-bye  passed", text)
        self.assertIn("Runs       1, 1 finished; median 15m 00s", text)
        self.assertIn("Slices     1 committed in 1 attempts (1.0 per slice)", text)
        self.assertIn("Refusals   1 in 3 steps; 1 fallback(s)", text)
        self.assertEqual(report.trends([]), "no runs\n")
