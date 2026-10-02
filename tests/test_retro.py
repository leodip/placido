import tempfile
import unittest
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
        text = retro.prompt(self.run.path, self.code, self.run.path / "steps" / "05-retro")
        self.assertIn(str(retro.SKILL), text)
        self.assertIn(f"**The run:** {self.run.path} (start with summary.md", text)
        self.assertIn(f"**The issue:** {self.run.path.parent}", text)
        self.assertIn(f"**The project:** {self.code}. Read it; change nothing.", text)
        self.assertIn("05-retro/retro.md", text)

    def test_the_retro_is_kept_in_the_run_folder(self):
        runner = FakeRunner(self.run, "# Retro: 02-x\n\nClean.\n\n## Suggestions\n\nNone.\n")
        result = retro.retro(runner, config.AgentSpec("claude", "opus", "xhigh"), self.run.path, self.code)
        self.assertEqual(result.outcome, "success")
        self.assertIn("Clean.", (self.run.path / "retro.md").read_text())
        self.assertEqual(runlog.last_event(self.run.path, "retro.done")["path"], str(self.run.path / "retro.md"))

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
    def test_follows_the_spec_role_unless_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('[roles.spec]\nagent = "codex"\neffort = "high"\n')
            self.assertEqual(config.load(cfg).roles["retro"].agent, config.AgentSpec("codex", "gpt-6.1-sol", "high"))
            cfg.write_text('[roles.retro]\nagent = "claude"\nmodel = "sonnet"\neffort = "medium"\n')
            self.assertEqual(config.load(cfg).roles["retro"].agent, config.AgentSpec("claude", "sonnet", "medium"))
