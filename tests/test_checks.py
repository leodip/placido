import subprocess
import tempfile
import unittest
from pathlib import Path

from placido import checks, config, driver, runlog
from placido.step import StepResult

RESULT = "Fix the final gates: add ok.txt\n\nThe gate needs it.\n\n## Evidence\n- passes\n"


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeFixer:
    """Each fix either repairs what the gate checks (a file ok.txt) or changes something else."""

    def __init__(self, run: runlog.Run, worktree: Path) -> None:
        self.run, self.worktree = run, worktree
        self.repairs: list[bool] = []
        self.prompts: list[str] = []

    def __call__(self, role, agent, task, name=None, check=None, ask=None, **kwargs):
        self.prompts.append(task)
        folder = self.run.step_dir(len(self.prompts), name)
        repair = self.repairs.pop(0) if self.repairs else True
        (self.worktree / ("ok.txt" if repair else f"try-{len(self.prompts)}.txt")).write_text("x\n")
        (folder / "result.md").write_text(RESULT)
        problems = check(folder / "result.md")
        return StepResult("invalid" if problems else "success", folder)


class FinalGatesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "user.email", "t@t")
        (self.repo / "README").write_text("hi\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        self.run = runlog.Run.create(self.tmp / "runs", "repo", "03-x", {})
        self.fixer = FakeFixer(self.run, self.repo)

    def ctx(self, final: str = "full") -> driver.Context:
        cfg = self.tmp / "config.toml"
        cfg.write_text(
            '[commands.full]\nrun = "test -f ok.txt"\n'
            f'[gates]\nfinal = [{f"{chr(34)}{final}{chr(34)}" if final else ""}]\n'
        )
        return driver.Context(self.run, self.fixer, None, config.load(cfg),
                              config.AgentSpec("claude", "opus", "high"), self.repo, {})

    def events(self) -> list[str]:
        return [e["event"] for e in runlog.read_events(self.run.path)]

    def test_no_final_gates(self):
        self.assertEqual(checks.final_gates(self.ctx(final=""), config.AgentSpec("claude", "opus", "high")), "none")

    def test_passing_gates(self):
        (self.repo / "ok.txt").write_text("x\n")
        self.assertEqual(checks.final_gates(self.ctx(), config.AgentSpec("claude", "opus", "high")), "passed")
        self.assertEqual(self.fixer.prompts, [])

    def test_a_failure_is_fixed_and_committed(self):
        outcome = checks.final_gates(self.ctx(), config.AgentSpec("claude", "opus", "high"))
        self.assertEqual(outcome, "passed")
        self.assertIn("Fix the final gates", self.fixer.prompts[0])
        self.assertIn("final-gates/1/gates/full.log", self.fixer.prompts[0])
        self.assertIn(str(checks.SKILL), self.fixer.prompts[0])
        body = sh(self.repo, "git", "log", "-1", "--format=%B")
        self.assertIn("Placido-Check-Fix: final-gates-1", body)
        self.assertEqual(self.events().count("final_gates.failed"), 1)
        self.assertIn("final_gates.passed", self.events())

    def test_gives_up_after_the_fixes(self):
        self.fixer.repairs = [False, False]
        outcome = checks.final_gates(self.ctx(), config.AgentSpec("claude", "opus", "high"))
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(self.fixer.prompts), 1)  # its fix fails the gate again, so nothing is committed
        self.assertEqual(sh(self.repo, "git", "log", "--format=%s"), "init")
        self.assertEqual(self.events().count("checks.fix_failed"), 1)

    def test_a_resumed_run_does_not_run_them_again(self):
        self.run.event("final_gates.passed", attempt=1)
        self.assertEqual(checks.final_gates(self.ctx(), config.AgentSpec("claude", "opus", "high")), "passed")
        self.assertNotIn("gate.start", self.events())
