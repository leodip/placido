import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from placido import config, retro, runlog
from placido.step import StepResult


class FakeRunner:
    def __init__(self, run: runlog.Run, text: str | None) -> None:
        self.run, self.text, self.calls = run, text, []

    def __call__(self, role, agent, task, name=None, check=None, **kwargs):
        self.calls.append((role, agent.agent, task))
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
        self.assertIn(f"**The run:** {self.run.path} (start with summary.md", text)
        self.assertIn(f"**The issue:** {self.run.path.parent}", text)
        self.assertIn(f"**The project:** {self.code}. Read it; change nothing.", text)
        self.assertIn("05-retro/retro.md", text)

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
        self.assertLess(text.index(f"  - 02-x: {self.run.path} (issue folder {self.run.path.parent})"),
                        text.index(f"  - 01-y: {older.path} (issue folder {older.path.parent}); an earlier"
                                   f" retro of it: {older.path / 'retro.md'}"))
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
