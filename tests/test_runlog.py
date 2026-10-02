import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from placido import runlog

START = datetime(2026, 10, 2, 15, 30, 12, 345000, tzinfo=timezone(timedelta(hours=-3)))


class FakeClock:
    """Starts at START and moves one second per reading."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        value = self.now
        self.now += timedelta(seconds=1)
        return value


class RunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def create(self, **kwargs) -> runlog.Run:
        return runlog.Run.create(
            self.root, "sandbox", "01-greet", {"placido": "0.0.1"}, clock=FakeClock(), **kwargs
        )


class RunsDirTest(unittest.TestCase):
    def test_default_is_under_home(self):
        self.assertEqual(runlog.runs_dir({}), Path.home() / "placido" / "runs")

    def test_environment_moves_it(self):
        self.assertEqual(runlog.runs_dir({"PLACIDO_RUNS_DIR": "/x/runs"}), Path("/x/runs"))

    def test_empty_variable_keeps_the_default(self):
        self.assertEqual(runlog.runs_dir({"PLACIDO_RUNS_DIR": ""}), runlog.runs_dir({}))


class CreateTest(RunTestCase):
    def test_folder_layout_and_run_json(self):
        run = self.create()
        self.assertEqual(run.path, self.root / "sandbox" / "01-greet" / "2026-10-02T153012")
        record = json.loads((run.path / "run.json").read_text())
        self.assertEqual(
            record,
            {
                "run_id": "2026-10-02T153012",
                "project": "sandbox",
                "issue": "01-greet",
                "started": "2026-10-02T15:30:12-03:00",
                "placido": "0.0.1",
            },
        )

    def test_same_second_gets_a_suffix(self):
        first, second = self.create(), self.create()
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(second.path.name, "2026-10-02T153012-2")
        self.assertEqual(json.loads((second.path / "run.json").read_text())["run_id"], second.path.name)

    def test_step_dir_is_numbered(self):
        step = self.create().step_dir(4, "implement-slice-2")
        self.assertTrue(step.is_dir())
        self.assertEqual(step.relative_to(step.parents[1]).as_posix(), "steps/04-implement-slice-2")


class EventTest(RunTestCase):
    def test_events_append_in_order_with_timestamps(self):
        run = self.create()
        run.event("run.start", project="sandbox")
        run.event("step.end", step="01-spec", outcome="success", seconds=12.5)
        events = list(runlog.read_events(run.path))
        self.assertEqual(
            events,
            [
                {"ts": "2026-10-02T15:30:13.345-03:00", "event": "run.start", "project": "sandbox"},
                {
                    "ts": "2026-10-02T15:30:14.345-03:00",
                    "event": "step.end",
                    "step": "01-spec",
                    "outcome": "success",
                    "seconds": 12.5,
                },
            ],
        )

    def test_reopened_run_keeps_appending(self):
        run = self.create()
        run.event("run.start")
        runlog.Run(run.path, clock=FakeClock()).event("run.resume")
        self.assertEqual([e["event"] for e in runlog.read_events(run.path)], ["run.start", "run.resume"])

    def test_echo_prints_the_readable_line(self):
        out = io.StringIO()
        run = self.create(echo=out)
        run.event("step.start", step="01-spec")
        self.assertEqual(out.getvalue(), "15:30:13  step.start    step=01-spec\n")

    def test_torn_last_line_is_skipped(self):
        run = self.create()
        run.event("run.start")
        with run.events_path.open("a") as log:
            log.write('{"ts": "2026-10-02T15:3')
        self.assertEqual([e["event"] for e in runlog.read_events(run.path)], ["run.start"])

    def test_missing_log_reads_as_empty(self):
        self.assertEqual(list(runlog.read_events(self.root)), [])


class FormatTest(unittest.TestCase):
    def test_fields_follow_the_name(self):
        line = runlog.format_event(
            {"ts": "2026-10-02T15:30:13.345-03:00", "event": "check.end", "exit": 0, "seconds": 1.5}
        )
        self.assertEqual(line, "15:30:13  check.end     exit=0 seconds=1.5")

    def test_text_with_spaces_is_quoted(self):
        line = runlog.format_event({"ts": "", "event": "fallback", "reason": "flagged for risk"})
        self.assertEqual(line, '  fallback      reason="flagged for risk"')

    def test_structured_values_are_json(self):
        line = runlog.format_event({"ts": "", "event": "x", "versions": {"pi": "1.0.0"}})
        self.assertIn('versions={"pi":', line)


class OutcomeTest(RunTestCase):
    def test_last_run_end_wins(self):
        run = self.create()
        run.event("run.end", outcome="failed")
        run.event("run.end", outcome="success")
        self.assertEqual(runlog.outcome(run.path), "success")

    def test_without_run_end_is_unfinished(self):
        run = self.create()
        run.event("run.start")
        self.assertEqual(runlog.outcome(run.path), "unfinished")


class ResolveRunTest(RunTestCase):
    def test_relative_name_resolves_under_the_runs_folder(self):
        self.assertEqual(
            runlog.resolve_run("sandbox/01-greet/x", self.root), self.root / "sandbox/01-greet/x"
        )

    def test_absolute_path_is_kept(self):
        self.assertEqual(runlog.resolve_run("/elsewhere/run", self.root), Path("/elsewhere/run"))


class LatestRunTest(RunTestCase):
    def test_none_when_empty(self):
        self.assertIsNone(runlog.latest_run(self.root))

    def test_newest_across_issues(self):
        old = self.create()
        new = runlog.Run.create(self.root, "other", "02-x", {}, clock=FakeClock())
        os.utime(old.path / "run.json", (1, 1))
        self.assertEqual(runlog.latest_run(self.root), new.path)
        self.assertEqual(runlog.all_runs(self.root), [new.path, old.path])


if __name__ == "__main__":
    unittest.main()
