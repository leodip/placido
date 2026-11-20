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
        self.unchanged: str | None = None  # a result saying nothing in the change causes the failure
        self.touch = False  # and a file written all the same

    def __call__(self, role, agent, task, name=None, check=None, ask=None, **kwargs):
        self.prompts.append(task)
        folder = self.run.step_dir(len(self.prompts), name)
        if self.unchanged is not None:
            if self.touch:
                (self.worktree / "try.txt").write_text("x\n")
            (folder / "result.md").write_text(self.unchanged)
        else:
            repair = self.repairs.pop(0) if self.repairs else True
            (self.worktree / ("ok.txt" if repair else f"try-{len(self.prompts)}.txt")).write_text("x\n")
            (folder / "result.md").write_text(RESULT)
        self.problems = check(folder / "result.md")
        return StepResult("invalid" if self.problems else "success", folder)


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

    def ctx(self, final: str = "full", last: str = "") -> driver.Context:
        cfg = self.tmp / "config.toml"
        cfg.write_text(
            '[commands.full]\nrun = "test -f ok.txt"\n'
            f'[gates]\nfinal = [{f"{chr(34)}{final}{chr(34)}" if final else ""}]\n'
            f'last = [{f"{chr(34)}{last}{chr(34)}" if last else ""}]\n'
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
        self.assertEqual(self.events().count("gate.start"), 2)  # the failure, and the fix's check; not again

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

    def test_no_last_gates(self):
        self.assertEqual(checks.last_gates(self.ctx(), config.AgentSpec("claude", "opus", "high")), "none")

    def test_a_failing_last_gate_is_fixed_and_committed(self):
        outcome = checks.last_gates(self.ctx(final="", last="full"), config.AgentSpec("claude", "opus", "high"))
        self.assertEqual(outcome, "passed")
        self.assertIn("Fix the last gates", self.fixer.prompts[0])
        self.assertIn("last-gates/1/gates/full.log", self.fixer.prompts[0])
        self.assertIn("Placido-Check-Fix: last-gates-1", sh(self.repo, "git", "log", "-1", "--format=%B"))
        self.assertIn("last_gates.passed", self.events())
        self.assertEqual(self.events().count("gate.start"), 2)

    def test_last_gates_that_still_fail_are_not_run_again(self):
        self.fixer.repairs = [False, False]
        ctx = self.ctx(final="", last="full")
        self.assertEqual(checks.last_gates(ctx, config.AgentSpec("claude", "opus", "high")), "failed")
        gave_up = [e for e in runlog.read_events(self.run.path) if e["event"] == "last_gates.gave_up"]
        self.assertEqual(gave_up[0]["gates"], ["full"])
        runs = self.events().count("gate.start")
        self.assertEqual(checks.last_gates(ctx, config.AgentSpec("claude", "opus", "high")), "failed")
        self.assertEqual(self.events().count("gate.start"), runs)  # a resumed delivery keeps the outcome

    def test_a_gate_with_arguments_is_not_its_bare_command(self):
        (self.repo / "ok.txt").write_text("x\n")  # full passes; full with an argument cannot
        ctx = self.ctx(final="", last='full", "full x')
        self.assertEqual(checks.last_gates(ctx, config.AgentSpec("claude", "opus", "high")), "failed")
        gave_up = [e for e in runlog.read_events(self.run.path) if e["event"] == "last_gates.gave_up"]
        self.assertEqual(gave_up[0]["gates"], ["full x"])

    def test_a_fixer_that_finds_the_cause_outside_the_change_changes_nothing(self):
        self.fixer.unchanged = ("No code changes\n\nmain fails the same e2e tests:\n#620 changed them.\n\n"
                                "## Evidence\n- the same failures on main\n")
        ctx = self.ctx(final="", last="full")
        self.assertEqual(checks.last_gates(ctx, config.AgentSpec("claude", "opus", "high")), "failed")
        self.assertEqual(self.fixer.problems, [])
        self.assertEqual(len(self.fixer.prompts), 1)
        # The fixer's check runs nothing (the gates failed on this very tree); placido then
        # runs the failed gates once more before it gives up.
        self.assertEqual(self.events().count("gate.start"), 2)
        self.assertTrue((self.run.path / "last-gates" / "2" / "gates" / "full.log").is_file())
        self.assertEqual(sh(self.repo, "git", "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(checks.outside(self.run.path, "the last gates"), "main fails the same e2e tests: #620 changed them.")
        self.assertEqual(self.events()[-2:], ["last_gates.failed", "last_gates.gave_up"])

    def test_a_flaky_gate_the_fixer_leaves_passes_when_run_once_more(self):
        self.fixer.unchanged = "No code changes\n\nA flaky test.\n\n## Evidence\n- x\n"
        flag = self.tmp / "ran-once"  # outside the worktree, which the fixer must leave as it was
        cfg = self.tmp / "flaky.toml"
        cfg.write_text(f'[commands.flaky]\nrun = "test -f {flag} || {{ touch {flag}; exit 1; }}"\n'
                       '[gates]\nlast = ["flaky"]\n')
        ctx = driver.Context(self.run, self.fixer, None, config.load(cfg),
                             config.AgentSpec("claude", "opus", "high"), self.repo, {})
        self.assertEqual(checks.last_gates(ctx, config.AgentSpec("claude", "opus", "high")), "passed")
        self.assertEqual(len(self.fixer.prompts), 1)
        passed = [e for e in runlog.read_events(self.run.path) if e["event"] == "last_gates.passed"]
        self.assertEqual([(e["attempt"], e.get("by")) for e in passed], [(2, "rerun")])
        self.assertNotIn("last_gates.gave_up", self.events())

    def test_a_fix_the_fixer_names_outside_the_change_is_kept_as_a_follow_up(self):
        self.fixer.unchanged = ("No code changes\n\nThe SSH tunnel to Mailpit dropped.\n\n## Follow-up\n"
                                "Keep the e2e tunnel alive\n\nlib.sh opens it with no keepalive:\nadd one.\n\n"
                                "## Evidence\n- x\n")
        checks.last_gates(self.ctx(final="", last="full"), config.AgentSpec("claude", "opus", "high"))
        self.assertEqual(checks.outside(self.run.path, "the last gates"), "The SSH tunnel to Mailpit dropped.")
        event = runlog.last_event(self.run.path, "checks.unchanged")
        self.assertEqual(event["label"], "last-gates-1")
        self.assertEqual(event["followup"], {"title": "Keep the e2e tunnel alive",
                                             "description": "lib.sh opens it with no keepalive:\nadd one."})

    def test_a_follow_up_section_needs_a_title_and_what_to_fix(self):
        self.fixer.unchanged = "No code changes\n\nmain's.\n\n## Follow-up\nOnly a title\n\n## Evidence\n- x\n"
        checks.last_gates(self.ctx(final="", last="full"), config.AgentSpec("claude", "opus", "high"))
        self.assertEqual(self.fixer.problems,
                         ["the '## Follow-up' section needs a title on its first line, then what to fix and where."])
        self.fixer.unchanged = "No code changes\n\nmain's.\n\n## Evidence\n- x\n"
        self.assertNotIn("followup", runlog.last_event(self.run.path, "checks.unchanged") or {})

    def test_the_reason_may_follow_on_the_first_line(self):
        result = self.tmp / "result.md"
        result.write_text("No code changes: main fails them too.\n\n## Evidence\n- x\n")
        self.assertEqual(checks.unchanged(result), "main fails them too.")
        result.write_text(RESULT)
        self.assertIsNone(checks.unchanged(result))

    def test_a_result_of_no_changes_beside_changes_is_sent_back(self):
        self.fixer.unchanged, self.fixer.touch = "No code changes\n\nmain's.\n", True
        self.assertEqual(checks.last_gates(self.ctx(final="", last="full"), config.AgentSpec("claude", "opus", "high")),
                         "failed")
        self.assertIn("the result says 'No code changes', but the worktree changed", self.fixer.problems[0])
        self.assertIsNone(checks.outside(self.run.path, "the last gates"))

    def test_the_fixer_is_given_the_projects_fix_notes(self):
        notes = self.repo / ".placido" / "notes" / "fix.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("# Fixing\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "notes")
        checks.final_gates(self.ctx(), config.AgentSpec("claude", "opus", "high"))
        self.assertIn(f"- **Project notes for this role:** {notes}", self.fixer.prompts[0])
