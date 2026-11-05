import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, config, runlog, spec, vet
from placido.step import StepResult
from tests.test_spec import AGREEMENT, SLICES, settings

FOUND = """# Agreement check: 02-many-names

## Findings

### V1 Decision 1 copies its precedent wrongly
- **Where:** Decisions, 1.
- **The code:** greet.py:12 dedupes.
- **Why it matters:** the run builds one of two things.
- **Correction:** a fact corrected.

### V2 Slice 2's gate is not in the Slices section
- **Where:** Slices.
- **The code:** n/a.
- **Why it matters:** the run gates it twice.
- **Correction:** a fact corrected.
"""

CLEAN = "# Agreement check: 02-many-names\n\n## Findings\n\nNone.\n"


class FakeRunner:
    """Runs a step by writing the vet's file, as an agent would, and records the call."""

    def __init__(self, run: runlog.Run, vetted: str | None, outcome: str = "success") -> None:
        self.run = run
        self.vetted = vetted
        self.outcome = outcome
        self.calls = []

    def __call__(self, role, agent, task, **kwargs) -> StepResult:
        self.calls.append((role, agent, task, kwargs))
        folder = kwargs["folder"]
        if self.vetted is not None:
            (folder / vet.FILE).write_text(self.vetted)
        problems = kwargs["check"](folder / "result.md")
        outcome = self.outcome if not problems else "invalid"
        return StepResult(outcome, folder)


class VetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = runlog.Run.create(self.root / "runs", "sandbox", "02-many-names", {})
        self.folder = spec.issue_dir(self.run.path)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.agent = config.AgentSpec("codex", "gpt-6.1-sol", "xhigh", "fast")

    def vet(self, runner: FakeRunner) -> list[str]:
        with redirect_stdout(io.StringIO()) as out:
            problems = vet.vet(runner, self.agent, self.run.path, self.worktree)
        self.printed = out.getvalue()
        return problems

    def events(self, kind: str) -> list[dict]:
        return [e for e in runlog.read_events(self.run.path) if e.get("event") == kind]


class PromptTest(VetTestCase):
    def test_names_the_skill_the_agreement_the_code_and_the_file(self):
        folder = self.root / "02-vet"
        text = vet.prompt(self.run.path, self.worktree, folder)
        self.assertIn(str(vet.SKILL), text)
        self.assertTrue(vet.SKILL.is_file())
        self.assertIn(str(self.folder / "agreement.md"), text)
        self.assertIn(str(self.folder / "slices.json"), text)
        self.assertIn(str(self.run.path / "issue.md"), text)
        self.assertIn(f"{self.worktree}. Read it; change nothing.", text)
        self.assertIn(str(folder / vet.FILE), text)
        self.assertIn("Project notes for this role:** none", text)

    def test_role_notes_when_present(self):
        notes = self.worktree / vet.NOTES
        notes.parent.mkdir(parents=True)
        notes.write_text("ours")
        self.assertIn(str(notes), vet.prompt(self.run.path, self.worktree, self.root / "f"))


class CheckTest(VetTestCase):
    def result(self, text: str | None) -> Path:
        folder = self.root / "step"
        folder.mkdir(exist_ok=True)
        if text is not None:
            (folder / vet.FILE).write_text(text)
        return folder / "result.md"

    def test_findings_or_none_pass(self):
        self.assertEqual(vet.check(self.result(FOUND)), [])
        self.assertEqual(vet.check(self.result(CLEAN)), [])

    def test_a_missing_or_empty_file(self):
        self.assertIn("write your findings", vet.check(self.result(None))[0])
        self.assertIn("write your findings", vet.check(self.result("  \n"))[0])

    def test_the_findings_section_is_required(self):
        (problem,) = vet.check(self.result("# Agreement check\n\nAll good.\n"))
        self.assertIn("'## Findings' section", problem)

    def test_count(self):
        self.assertEqual(vet.count(self.result(FOUND).parent / vet.FILE), 2)
        self.assertEqual(vet.count(self.result(CLEAN).parent / vet.FILE), 0)
        self.assertEqual(vet.count(self.root / "nowhere.md"), 0)


class VetStepTest(VetTestCase):
    def test_findings_go_back_to_the_interview(self):
        runner = FakeRunner(self.run, FOUND)
        (problem,) = self.vet(runner)
        ((role, agent, task, kwargs),) = runner.calls
        folder = kwargs["folder"]
        self.assertEqual((role, agent), ("vet", self.agent))
        self.assertEqual(folder.name, "01-vet")
        self.assertIn(str(folder / vet.FILE), task)
        self.assertEqual(kwargs["confined"], (self.worktree, self.folder))
        self.assertIn("found 2 problems, in " + str(folder / vet.FILE), problem)
        self.assertIn("ask the user as an interview question first", problem)
        self.assertEqual([e["findings"] for e in self.events("vet.done")], [2])

    def test_one_finding_reads_as_one(self):
        one = FOUND.split("### V2")[0]
        (problem,) = self.vet(FakeRunner(self.run, one))
        self.assertIn("found 1 problem, in", problem)

    def test_nothing_found_seals(self):
        self.assertEqual(self.vet(FakeRunner(self.run, CLEAN)), [])
        self.assertEqual([e["findings"] for e in self.events("vet.done")], [0])

    def test_a_check_that_cannot_run_does_not_hold_the_seal(self):
        self.assertEqual(self.vet(FakeRunner(self.run, None, outcome="quota")), [])
        self.assertIn("sealing it unchecked", self.printed)
        self.assertEqual([e["outcome"] for e in self.events("vet.skipped")], ["invalid"])
        self.assertEqual(self.events("vet.done"), [])


class SpecCheckTest(VetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = settings()
        self.vetted = []

    def check(self, answer: list[str]):
        return vet.spec_check(self.folder, self.settings, lambda: self.vetted.append(1) or answer)

    def write(self) -> None:
        (self.folder / "agreement.md").write_text(AGREEMENT)
        (self.folder / "slices.json").write_text(json.dumps(SLICES))

    def test_the_agreement_s_own_problems_come_first_and_hold_the_vet(self):
        check = self.check(["vet finding"])
        problems = check(self.root / "result.md")
        self.assertTrue(any("missing" in p for p in problems))
        self.assertEqual(self.vetted, [])

    def test_vets_once_the_agreement_passes_and_only_once(self):
        self.write()
        check = self.check(["vet finding"])
        self.assertEqual(check(self.root / "result.md"), ["vet finding"])
        self.assertEqual(check(self.root / "result.md"), [])  # settled by the interview: sealed
        self.assertEqual(self.vetted, [1])

    def test_a_later_problem_of_the_agreement_s_own_is_still_caught(self):
        self.write()
        check = self.check([])
        self.assertEqual(check(self.root / "result.md"), [])
        (self.folder / "slices.json").write_text("{")
        self.assertTrue(check(self.root / "result.md"))
        self.assertEqual(self.vetted, [1])


class Interview:
    """`placido spec`'s runner: the interview writes the agreement and its result, and
    settles the check's findings when they come back; the vet writes its file."""

    def __init__(self, run: runlog.Run, folder: Path, vetted: str) -> None:
        self.run = run
        self.folder = folder
        self.vetted = vetted
        self.roles = []
        self.sent_back = []

    def status(self, text: str) -> None:
        pass

    def __call__(self, role, agent, task, **kwargs) -> StepResult:
        self.roles.append(role)
        if role == "vet":
            folder = kwargs["folder"]
            (folder / vet.FILE).write_text(self.vetted)
            return StepResult("success" if not kwargs["check"](folder / "result.md") else "invalid", folder)
        (self.folder / "agreement.md").write_text(AGREEMENT)
        (self.folder / "slices.json").write_text(json.dumps(SLICES))
        folder = self.run.new_step_dir(role)
        for _ in range(3):
            problems = kwargs["check"](folder / "result.md")
            if not problems:
                return StepResult("success", folder)
            self.sent_back.append(problems)
        return StepResult("invalid", folder)


class SpecCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / ".placido").mkdir()
        (self.repo / ".placido" / "config.toml").write_text('[commands.data]\nrun = "y"\n')
        self.run = runlog.Run.create(self.root / "runs", "repo", "02-x", {})
        self.run.event("worktree.created", path=str(self.repo), workspace="w1")
        self.run.event("run.ready")
        self.folder = spec.issue_dir(self.run.path)

    def spec(self, vetted: str) -> Interview:
        interview = Interview(self.run, self.folder, vetted)
        env = {"PLACIDO_RUNS_DIR": str(self.root / "runs"), "PLACIDO_USER_CONFIG": str(self.root / "none.toml")}
        with mock.patch.dict(os.environ, env), mock.patch.object(Path, "cwd", return_value=self.repo), \
                mock.patch.object(cli, "_runner", return_value=(self.run, interview)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["spec"]), 0)
        self.assertTrue(spec.sealed(self.folder))
        return interview

    def test_the_agreement_is_checked_before_it_is_sealed(self):
        interview = self.spec(CLEAN)
        self.assertEqual(interview.roles, ["spec", "vet"])
        self.assertEqual(interview.sent_back, [])

    def test_findings_go_to_the_interview_and_the_seal_waits_for_them(self):
        interview = self.spec(FOUND)
        self.assertEqual(interview.roles, ["spec", "vet"])
        ((problem,),) = interview.sent_back
        self.assertIn("found 2 problems", problem)


class RoleTest(unittest.TestCase):
    def test_another_vendor_than_the_interview_by_default(self):
        roles = settings().roles
        self.assertEqual(roles["vet"].agent, config.AgentSpec("codex", "gpt-6.1-sol", "xhigh", "fast"))
        self.assertNotEqual(roles["vet"].agent.agent, roles["spec"].agent.agent)

    def test_a_project_can_change_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[roles.vet]\nagent = "claude"\neffort = "high"\n')
            self.assertEqual(config.load(path).roles["vet"].agent.agent, "claude")


if __name__ == "__main__":
    unittest.main()
