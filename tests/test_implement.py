import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from placido import config, implement, mutate, runlog, spec

SLICES = [
    {"id": 1, "title": "Prefactor", "delivers": "a", "blocked_by": [], "gates": []},
    {"id": 2, "title": "Feature", "delivers": "b", "blocked_by": [1], "gates": ["data"]},
    {"id": 3, "title": "Docs", "delivers": "c", "blocked_by": [], "gates": []},
]

RESULT = """Greet several names

greet.py takes one or more names and greets each on its own line.

## Evidence
- **Red:** 3 failures.
- **Green:** unit passed.

## Follow-ups
- None.
"""


def settings(text: str) -> config.Config:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(text)
        return config.load(path)


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ChooseTest(unittest.TestCase):
    def test_first_ready_slice_in_order(self):
        self.assertEqual(implement.choose(SLICES, set())["id"], 1)
        self.assertEqual(implement.choose(SLICES, {1})["id"], 2)
        self.assertEqual(implement.choose(SLICES, {1, 2})["id"], 3)

    def test_named_slice(self):
        self.assertEqual(implement.choose(SLICES, set(), 3)["id"], 3)

    def test_named_slice_refusals(self):
        with self.assertRaisesRegex(implement.ImplementError, "blocked by 1"):
            implement.choose(SLICES, set(), 2)
        with self.assertRaisesRegex(implement.ImplementError, "already committed"):
            implement.choose(SLICES, {1}, 1)
        with self.assertRaisesRegex(implement.ImplementError, "no slice 9"):
            implement.choose(SLICES, set(), 9)

    def test_all_done(self):
        with self.assertRaisesRegex(implement.ImplementError, "every slice is committed"):
            implement.choose(SLICES, {1, 2, 3})


class ResultTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.result = Path(tmp.name) / "result.md"

    def test_message_is_everything_above_the_evidence(self):
        self.result.write_text(RESULT)
        self.assertEqual(
            implement.message(self.result),
            "Greet several names\n\ngreet.py takes one or more names and greets each on its own line.",
        )

    def test_blocked(self):
        self.result.write_text("Blocked: should blank names be errors?\n\nThe agreement says...\n")
        self.assertEqual(implement.blocked(self.result), "should blank names be errors?")
        self.result.write_text(RESULT)
        self.assertIsNone(implement.blocked(self.result))


class SliceTestCase(unittest.TestCase):
    """A real repository with a worktree change, gates that really run, and a run folder."""

    CONFIG = (
        '[commands.unit]\nrun = "test -f feature.txt"\n'
        '[commands.data]\nrun = "echo data checked; test -n \\"$PLACIDO_NAME\\""\n'
        '[gates]\nslice = ["unit"]\n[implement]\nmutations = 1\n'
    )

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
        self.head = sh(self.repo, "git", "rev-parse", "HEAD")
        self.settings = settings(self.CONFIG)
        self.run = runlog.Run.create(self.tmp / "runs", "repo", "02-x", {})
        self.step = self.run.step_dir(1, "implement-slice-2")
        self.result = self.step / "result.md"
        self.result.write_text(RESULT)
        self.env = {"PLACIDO_NAME": "repo-2"}

    def kill(self, n: int = 1, same: bool = False) -> None:
        for i in range(n):
            new = "b" if same else f"b{i}"
            mutate.record(self.step, Path("f"), "a", new, ["t"], mutate.Mutation("killed", 1, 0.1, ""))

    def check(self) -> list[str]:
        return implement.check(self.result, self.repo, self.head, SLICES[1], self.settings, self.env, self.run)


class CheckTest(SliceTestCase):
    def test_passes_and_runs_slice_and_extra_gates(self):
        (self.repo / "feature.txt").write_text("done\n")
        self.kill()
        self.assertEqual(self.check(), [])
        ends = [e for e in runlog.read_events(self.run.path) if e["event"] == "gate.end"]
        self.assertEqual([(e["gate"], e["exit"]) for e in ends], [("unit", 0), ("data", 0)])
        self.assertEqual((self.step / "gates" / "data.log").read_text(), "data checked\n")

    def test_failing_gate_is_a_problem(self):
        (self.repo / "other.txt").write_text("x\n")
        self.kill()
        (problem,) = self.check()
        self.assertIn("gate unit (`test -f feature.txt`) failed with exit 1", problem)
        self.assertIn(str(self.step / "gates" / "unit.log"), problem)

    def test_no_changes(self):
        self.kill()
        self.assertIn("the worktree has no changes.", self.check())

    def test_too_few_mutations_and_no_gates_run(self):
        (self.repo / "feature.txt").write_text("done\n")
        problems = self.check()
        self.assertEqual(problems, ["0 mutations killed; the slice needs 1. Use `placido mutate` as your prompt shows."])
        self.assertNotIn("gate.start", [e["event"] for e in runlog.read_events(self.run.path)])

    def test_repeating_a_mutation_counts_once(self):
        self.settings = settings(self.CONFIG.replace("mutations = 1", "mutations = 2"))
        (self.repo / "feature.txt").write_text("done\n")
        self.kill(2, same=True)
        self.assertEqual(self.check(), ["1 mutation killed; the slice needs 2. Use `placido mutate` as your prompt shows."])
        self.kill(2)
        self.assertEqual(self.check(), [])

    def test_survivors_do_not_count(self):
        (self.repo / "feature.txt").write_text("done\n")
        mutate.record(self.step, Path("f"), "a", "b", ["t"], mutate.Mutation("survived", 0, 0.1, ""))
        self.assertIn("0 mutations killed", self.check()[0])

    def test_agent_commit_is_caught(self):
        (self.repo / "feature.txt").write_text("done\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "agent did it")
        (self.repo / "more.txt").write_text("x\n")
        self.kill()
        problems = self.check()
        self.assertIn(f"`git reset --soft {self.head}`", problems[0])

    def test_result_shape(self):
        (self.repo / "feature.txt").write_text("done\n")
        self.kill()
        self.result.write_text("x" * 80 + "\n\nbody\n")
        problems = self.check()
        self.assertIn("the commit subject is 80 characters; keep it to 72.", problems)
        self.assertIn("the result has no '## Evidence' section.", problems)

    def test_blocked_result_skips_the_checks(self):
        self.result.write_text("Blocked: which way?\n")
        self.assertEqual(self.check(), [])

    def test_mutations_off(self):
        self.settings = settings(self.CONFIG.replace("mutations = 1", "mutations = 0"))
        (self.repo / "feature.txt").write_text("done\n")
        self.assertEqual(self.check(), [])


class CommitTest(SliceTestCase):
    def test_commit_uses_the_result_and_links_the_run(self):
        (self.repo / "feature.txt").write_text("done\n")
        sha = implement.commit(self.repo, self.result, SLICES[1], self.run)
        self.assertEqual(sha, sh(self.repo, "git", "rev-parse", "HEAD"))
        body = sh(self.repo, "git", "log", "-1", "--format=%B")
        self.assertTrue(body.startswith("Greet several names\n\ngreet.py takes one or more names"))
        self.assertIn(f"Placido-Run: 02-x/{self.run.path.name}", body)
        self.assertIn("Placido-Slice: 2", body)
        self.assertNotIn("Evidence", body)
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        self.assertEqual(implement.committed(self.run.path), {2})


class PromptTest(SliceTestCase):
    def test_names_the_slice_gates_and_mutations(self):
        text = implement.prompt(self.run.path, self.repo, SLICES[1], self.settings)
        self.assertIn(f"Follow the placido implement skill in {implement.SKILL}.", text)
        self.assertTrue(implement.SKILL.is_file())
        self.assertIn(str(spec.issue_dir(self.run.path) / "agreement.md"), text)
        self.assertIn('**This slice:** 2, "Feature": b', text)
        self.assertIn("**Tests first:** yes.", text)
        self.assertIn("**Gates for this slice:** unit, data.", text)
        self.assertIn("at least 1 killed", text)
        self.assertIn("- **unit**, `test -f feature.txt`", text)


class OverrideTest(unittest.TestCase):
    base = config.AgentSpec("claude", "opus", "high")

    def test_agent_alone_takes_its_default_model(self):
        self.assertEqual(
            config.override(self.base, "codex", None, None), config.AgentSpec("codex", "gpt-6.1-sol", "high")
        )

    def test_model_and_effort(self):
        self.assertEqual(
            config.override(self.base, None, "sonnet", "low"), config.AgentSpec("claude", "sonnet", "low")
        )

    def test_nothing_given_keeps_the_role(self):
        self.assertEqual(config.override(self.base, None, None, None), self.base)

    def test_bad_values(self):
        with self.assertRaisesRegex(config.ConfigError, "--agent 'gemini' is not one of"):
            config.override(self.base, "gemini", None, None)
        with self.assertRaisesRegex(config.ConfigError, "--effort 'huge' is not one of"):
            config.override(self.base, None, None, "huge")


if __name__ == "__main__":
    unittest.main()
