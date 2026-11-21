import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from placido import config, retro, runlog, status
from placido.herdr import HerdrError
from placido.step import StepResult


class FakeRunner:
    def __init__(self, run: runlog.Run, text: str | None, error: Exception | None = None) -> None:
        self.run, self.text, self.calls, self.error = run, text, [], error

    def __call__(self, role, agent, task, name=None, check=None, **kwargs):
        self.calls.append((role, agent.agent, task))
        status.show(self.run.path, "working · retro")  # as the step does while it works
        if self.error is not None:
            raise self.error
        folder = self.run.step_dir(1, name)
        if self.text is not None:
            (folder / "retro.md").write_text(self.text)
        (folder / "result.md").write_text("1 suggestion\n")
        problems = check(folder / "result.md")
        return StepResult("invalid" if problems else "success", folder)


class RetroTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = runlog.Run.create(Path(tmp.name), "proj", "02-x", {})
        self.code = Path(tmp.name) / "code"
        self.code.mkdir()

    def test_prompt_points_at_the_run_the_issue_and_the_code(self):
        text = retro.prompt([self.run.path], self.code, self.run.path / "steps" / "05-retro")
        self.assertIn(str(retro.SKILL), text)
        self.assertIn(f"**The run:** {self.run.path} (outcome: unfinished; start with summary.md", text)
        self.assertIn(f"**The issue:** {self.run.path.parent}", text)
        self.assertIn(f"**The project:** {self.code}. Read it; change nothing.", text)
        self.assertIn("05-retro/retro.md", text)

    def test_a_deferred_run_is_named_as_deferred_with_its_trigger(self):
        self.run.event("run.end", outcome="deferred", reason="start once tool 2.16.0 is out")
        text = retro.prompt([self.run.path], self.code, self.run.path / "steps" / "05-retro")
        self.assertIn(f"**The run:** {self.run.path} (outcome: deferred, nothing built: start once tool 2.16.0 is"
                      " out; start with summary.md", text)
        self.assertIn("A run whose outcome is **deferred** ended where its interview chose to", retro.SKILL.read_text())

    def test_the_retro_is_kept_in_the_run_folder(self):
        runner = FakeRunner(self.run, "# Retro: 02-x\n\nClean.\n\n## Suggestions\n\nNone.\n")
        result = retro.retro(runner, config.AgentSpec("claude", "opus", "xhigh"), [self.run.path], self.code)
        self.assertEqual(result.outcome, "success")
        self.assertIn("Clean.", (self.run.path / "retro.md").read_text())
        self.assertEqual(runlog.last_event(self.run.path, "retro.done")["path"], str(self.run.path / "retro.md"))

    def test_several_runs_are_looked_back_on_together_newest_first(self):
        root = self.run.path.parent.parent.parent
        older = runlog.Run.create(root, "proj", "01-y", {})
        (older.path / "retro.md").write_text("# Retro: 01-y\n")
        text = retro.prompt([self.run.path, older.path], self.code, Path("/r/steps/01-retro"))
        self.assertIn("# Retro on the last 2 runs of proj", text)
        self.assertIn("these runs are looked back on together", text)
        self.assertLess(text.index(f"  - 02-x: {self.run.path} (outcome: unfinished; issue folder {self.run.path.parent})"),
                        text.index(f"  - 01-y: {older.path} (outcome: unfinished; issue folder {older.path.parent});"
                                   f" an earlier retro of it: {older.path / 'retro.md'}"))
        self.assertNotIn("an earlier retro of it: " + str(self.run.path), text)
        self.assertIn("/r/steps/01-retro/retro.md", text)

    def test_a_retro_on_several_runs_is_kept_in_a_folder_no_run_listing_shows(self):
        root = self.run.path.parent.parent.parent
        clock = lambda: datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
        folder = retro.folder(root, "proj", clock)
        self.assertEqual(folder, root / "proj" / "retros" / "2026-11-20T093000")
        self.assertEqual(retro.folder(root, "proj", clock).name, "2026-11-20T093000-2")
        host = runlog.Run(folder)
        runner = FakeRunner(host, "# Retro: the last 2\n\n## Suggestions\n\nNone.\n")
        older = runlog.Run.create(root, "proj", "01-y", {})
        result = retro.retro(runner, config.AgentSpec("claude", "opus", "xhigh"), [self.run.path, older.path], self.code)
        self.assertEqual(result.outcome, "success")
        self.assertIn("the last 2", (folder / "retro.md").read_text())
        self.assertFalse((self.run.path / "retro.md").exists())
        done = runlog.last_event(folder, "retro.done")
        self.assertEqual(done["runs"], [str(self.run.path), str(older.path)])
        self.assertNotIn(folder, runlog.all_runs(root))

    def retro_of_several(self, runner_for) -> Path:
        root = self.run.path.parent.parent.parent
        folder = retro.folder(root, "proj")
        runner = runner_for(runlog.Run(folder))
        try:
            retro.retro(runner, config.AgentSpec("claude", "opus", "high"), [self.run.path], self.code)
        except HerdrError:
            pass
        return folder

    def test_a_retro_s_folder_ends_done_or_stopped_not_working(self):
        # A retro of the last six runs finished on 2026-11-21, its folder still `working · retro`.
        done = self.retro_of_several(lambda host: FakeRunner(host, "# Retro\n\n## Suggestions\n\nNone.\n"))
        self.assertEqual(status.read(done), "done · retro")
        refused = self.retro_of_several(lambda host: FakeRunner(host, None))
        self.assertEqual(status.read(refused), "stopped · retro invalid")
        broken = self.retro_of_several(lambda host: FakeRunner(host, None, HerdrError("tab gone")))
        self.assertEqual(status.read(broken), "stopped · retro failed")

    def test_a_retro_whose_report_cannot_be_kept_is_not_done(self):
        # Codex, reviewing 8a02533: the status said done once the step did, before the copy.
        with mock.patch.object(retro.shutil, "copyfile", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self.retro_of_several(lambda host: FakeRunner(host, "# Retro\n\n## Suggestions\n\nNone.\n"))
        (folder,) = (self.run.path.parent.parent / "retros").iterdir()
        self.assertEqual(status.read(folder), "stopped · retro failed")
        self.assertIsNone(runlog.last_event(folder, "retro.done"))

    def test_a_run_s_own_folder_gets_its_status_back(self):
        status.show(self.run.path, "you · the last gates fail on pull request 889")
        retro.retro(FakeRunner(self.run, "# Retro\n\n## Suggestions\n\nNone.\n"),
                    config.AgentSpec("claude", "opus", "high"), [self.run.path], self.code)
        self.assertEqual(status.read(self.run.path), "you · the last gates fail on pull request 889")
        (self.run.path / status.FILE).unlink()
        with self.assertRaises(HerdrError):
            retro.retro(FakeRunner(self.run, None, HerdrError("tab gone")),
                        config.AgentSpec("claude", "opus", "high"), [self.run.path], self.code)
        self.assertFalse((self.run.path / status.FILE).exists())  # it had none, and gets none

    def test_a_missing_or_shapeless_retro_is_sent_back(self):
        for text, problem in ((None, "write the retro"), ("# Retro\n\nfine\n", "'## Suggestions'")):
            with self.subTest(text=text):
                folder = self.run.step_dir(9, f"retro-{problem[:5]}")
                if text is not None:
                    (folder / "retro.md").write_text(text)
                (problems,) = [retro.check(folder / "result.md")]
                self.assertIn(problem, problems[0])
        self.assertFalse((self.run.path / "retro.md").exists())


class RetroRoleTest(unittest.TestCase):
    def test_opus_by_default_whatever_the_interview_runs_on(self):
        # A retro reads whole runs, so it does not follow the interview onto Fable.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            self.assertEqual(config.load(None).roles["spec"].agent.model, "fable")
            self.assertEqual(config.load(None).roles["retro"].agent, config.AgentSpec("claude", "opus", "high"))
            cfg.write_text('[roles.spec]\nagent = "codex"\neffort = "high"\n')
            self.assertEqual(config.load(cfg).roles["retro"].agent, config.AgentSpec("claude", "opus", "high"))
            cfg.write_text('[roles.retro]\nagent = "claude"\nmodel = "sonnet"\neffort = "medium"\n')
            self.assertEqual(config.load(cfg).roles["retro"].agent, config.AgentSpec("claude", "sonnet", "medium"))
