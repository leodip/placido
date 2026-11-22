import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, config, github, runlog, spec, status, vet
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
        self.agent = config.AgentSpec("codex", "gpt-6.2-sol", "xhigh", "fast")

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
        self.assertNotIn("confined", kwargs)  # it runs as freely as the review (2026-11-21)
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

    def test_a_deferral_skips_the_agreement_and_the_vet(self):
        check = self.check(["vet finding"])
        result = self.root / "result.md"
        result.write_text("Deferred: start once tool 2.16.0 is out\n\nIt bundles the fix.\n\n## Evidence\n- x\n")
        self.assertEqual(check(result), [])  # no agreement is written, and none is checked
        result.write_text("Deferred: start once tool 2.16.0 is out\n\nIt bundles the fix.\n")
        self.assertEqual(len(check(result)), 1)
        self.assertEqual(self.vetted, [])

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


DEFERRAL = ("Deferred: start once tool 2.16.0 is out\n\nThe pinned 2.15.0 bundles the old analyzer; 2.16.0 the fix.\n\n"
            "## Evidence\n- 10 of 12 cold runs failed in the devcontainer.\n")


class Deferring(Interview):
    """An interview that settles with the user that nothing can be built yet."""

    def status(self, text: str) -> None:
        status.show(self.run.path, text)

    text = DEFERRAL  # the result the agent writes
    check = True  # False: the agent quit right after writing, and the step ran no check

    def __call__(self, role, agent, task, **kwargs) -> StepResult:
        self.roles.append(role)
        folder = self.run.new_step_dir(role)
        (folder / "result.md").write_text(self.text)
        if not self.check:
            return StepResult("success", folder)
        return StepResult("invalid" if kwargs["check"](folder / "result.md") else "success", folder)


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

    def defer(self, code: int = 0) -> tuple[Deferring, str, str]:
        interview = Deferring(self.run, self.folder, CLEAN)
        user = self.root / "user.toml"
        user.write_text("[spec]\nthen_run = true\n")  # a deferred run never goes on
        env = {"PLACIDO_RUNS_DIR": str(self.root / "runs"), "PLACIDO_USER_CONFIG": str(user)}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(Path, "cwd", return_value=self.repo), \
                mock.patch.object(cli, "_runner", return_value=(self.run, interview)), \
                mock.patch.object(cli, "cmd_run", side_effect=AssertionError("placido run")), \
                mock.patch("sys.stderr", err), redirect_stdout(out):
            self.assertEqual(cli.main(["spec"]), code)
        return interview, out.getvalue(), err.getvalue()

    def test_a_deferral_ends_the_run_as_deferred(self):
        # #494: the interview settled that it waits for an upstream release, and the run
        # had no way to end but the agent quitting and placido close calling it abandoned.
        interview, out, err = self.defer()
        self.assertEqual(interview.roles, ["spec"])  # nothing to vet
        self.assertFalse(spec.sealed(self.folder))
        deferred = runlog.last_event(self.run.path, "spec.deferred")
        self.assertEqual(deferred["trigger"], "start once tool 2.16.0 is out")
        self.assertTrue(deferred["result"].endswith("-spec/result.md"))
        end = runlog.last_event(self.run.path, "run.end")
        self.assertEqual((end["outcome"], end["reason"]), ("deferred", "start once tool 2.16.0 is out"))
        self.assertEqual(runlog.outcome(self.run.path), "deferred")
        self.assertEqual(status.read(self.run.path), "done · deferred: start once tool 2.16.0 is out")
        self.assertIn("- **State:** deferred, nothing built: start once tool 2.16.0 is out\n",
                      (self.run.path / "summary.md").read_text())
        self.assertIn("run deferred, nothing built: start once tool 2.16.0 is out", out)
        self.assertIsNone(runlog.last_event(self.run.path, "deferral.posted"))  # the issue is not on GitHub
        self.assertEqual(err, "")

    def test_a_deferred_run_takes_no_more_steps(self):
        self.defer()
        for command in (["spec"], ["run"], ["review"]):
            err = io.StringIO()
            env = {"PLACIDO_RUNS_DIR": str(self.root / "runs"), "PLACIDO_USER_CONFIG": str(self.root / "none.toml")}
            with self.subTest(command=command), mock.patch.dict(os.environ, env), \
                    mock.patch.object(Path, "cwd", return_value=self.repo), \
                    mock.patch.object(cli, "_runner", side_effect=AssertionError("a step ran")), \
                    mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(command), 1)
            self.assertIn("this run ended deferred (start once tool 2.16.0 is out); close it with placido close",
                          err.getvalue())

    def test_an_incomplete_deferral_the_check_missed_ends_nothing(self):
        # Codex, reviewing 444aead: an agent that quits right after writing its result
        # ends the step with no check run, and the run was deferred on a bare header.
        interview = Deferring(self.run, self.folder, CLEAN)
        interview.text, interview.check = "Deferred:\n", False
        err = io.StringIO()
        env = {"PLACIDO_RUNS_DIR": str(self.root / "runs"), "PLACIDO_USER_CONFIG": str(self.root / "none.toml")}
        with mock.patch.dict(os.environ, env), mock.patch.object(Path, "cwd", return_value=self.repo), \
                mock.patch.object(cli, "_runner", return_value=(self.run, interview)), \
                mock.patch.object(github, "post_deferral", side_effect=AssertionError("posted")), \
                mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["spec"]), 1)
        self.assertIn("the interview's deferral is incomplete, so the run is not deferred", err.getvalue())
        self.assertEqual(runlog.outcome(self.run.path), "unfinished")
        self.assertIsNone(runlog.last_event(self.run.path, "spec.deferred"))
        self.assertEqual(status.read(self.run.path), "stopped · spec deferral incomplete")

    def test_an_interrupted_post_leaves_the_run_deferred(self):
        # Codex, reviewing 444aead: run.end came after the post, so an interrupt left none,
        # and placido close then called the run abandoned. Reviewing 0051abb: it then said
        # to run placido spec again, which refuses a deferred run, and wrote no summary.
        with mock.patch.object(github, "post_deferral", side_effect=KeyboardInterrupt):
            _, _, err = self.defer(code=130)
        self.assertEqual(runlog.outcome(self.run.path), "deferred")
        self.assertEqual(status.read(self.run.path), "done · deferred: start once tool 2.16.0 is out")
        self.assertEqual(runlog.last_event(self.run.path, "deferral.post_failed")["error"], "interrupted")
        self.assertIn("posting it on the issue failed: interrupted. It may have gone out before the interrupt:"
                      " look at the issue first.", err)
        self.assertNotIn("Run the same command again", err)
        self.assertIn("posting it on the issue failed: interrupted", (self.run.path / "summary.md").read_text())

    def test_a_deferral_that_cannot_be_posted_says_how_to_post_it(self):
        with mock.patch.object(github, "post_deferral", side_effect=github.GitHubError("gh issue comment: HTTP 502")):
            _, _, err = self.defer()
        self.assertEqual(runlog.last_event(self.run.path, "deferral.post_failed")["error"], "gh issue comment: HTTP 502")
        self.assertEqual(runlog.outcome(self.run.path), "deferred")
        self.assertIn("posting it on the issue failed: gh issue comment: HTTP 502", err)
        self.assertIn("posting it on the issue failed: gh issue comment: HTTP 502",
                      (self.run.path / "summary.md").read_text())

    def test_findings_go_to_the_interview_and_the_seal_waits_for_them(self):
        interview = self.spec(FOUND)
        self.assertEqual(interview.roles, ["spec", "vet"])
        ((problem,),) = interview.sent_back
        self.assertIn("found 2 problems", problem)


class PostByHandTest(unittest.TestCase):
    """What placido prints when a deferral's post fails."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Codex, reviewing 444aead: a runs folder with a space split the printed --body-file.
        self.run = runlog.Run.create(Path(tmp.name) / "my runs", "repo", "494-x", {
            "issue_number": 494, "issue_source": "https://github.com/o/r/issues/494"})
        self.result = self.run.path / "steps" / "01-spec" / "result.md"

    def test_the_command_is_quoted_for_the_shell(self):
        body = self.run.path / github.DEFERRAL_COMMENT
        body.write_text("**Deferred:** x\n")
        said = cli._post_by_hand(self.run, self.result)
        self.assertTrue(said.startswith("post it with: "))
        self.assertEqual(shlex.split(said.removeprefix("post it with: ")),
                         ["gh", "issue", "comment", "494", "-R", "o/r", "--body-file", str(body)])

    def test_without_the_comment_s_text_it_points_at_the_result(self):
        self.assertEqual(cli._post_by_hand(self.run, self.result),
                         f"its text is the trigger and the why, above ## Evidence, in {self.result}")


class RoleTest(unittest.TestCase):
    def test_another_vendor_than_the_interview_by_default(self):
        roles = settings().roles
        self.assertEqual(roles["vet"].agent, config.AgentSpec("agy", "gemini-4-neon", "high"))
        self.assertNotEqual(roles["vet"].agent.agent, roles["spec"].agent.agent)

    def test_a_project_can_change_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[roles.vet]\nagent = "claude"\neffort = "high"\n')
            self.assertEqual(config.load(path).roles["vet"].agent.agent, "claude")


if __name__ == "__main__":
    unittest.main()
