import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, config, runlog, spec

AGREEMENT = """# Agreement: Greet several names

Issue: issues/02-many-names.md

## Problem
One name at a time.

## Solution
Several names, one line each.

## Decisions
1. **Greet each occurrence.** Literal reading. Rejected: dedupe, because the issue never asks.

## Seams
- **The command's output:** what users see.

## Testing
Through main().

## Slices
1. **Accept several names** — prints one line per name. Blocked by: none. Gates: slice gates only.
2. **Reject empty names** — exits 2 on an empty name. Blocked by: 1. Gates: slice gates and data.

## Out of scope
Nothing else.
"""

SLICES = {
    "slices": [
        {"id": 1, "title": "Accept several names", "delivers": "one line per name", "blocked_by": [], "gates": []},
        {"id": 2, "title": "Reject empty names", "delivers": "exit 2", "blocked_by": [1], "gates": ["data"]},
    ]
}


def settings(text: str = '[commands.unit]\nrun = "x"\n[commands.data]\nrun = "y"\n[gates]\nslice = ["unit"]\n'):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(text)
        return config.load(path)


class SpecTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = runlog.Run.create(
            self.root / "runs", "sandbox", "02-many-names",
            {"issue_source": "/repo/issues/02-many-names.md", "branch": "placido/02-many-names"},
        )
        self.folder = spec.issue_dir(self.run.path)
        self.settings = settings()

    def write(self, agreement: str = AGREEMENT, slices: dict | str = SLICES) -> None:
        (self.folder / "agreement.md").write_text(agreement)
        text = slices if isinstance(slices, str) else json.dumps(slices)
        (self.folder / "slices.json").write_text(text)

    def problems(self, **kwargs) -> list[str]:
        self.write(**kwargs)
        return spec.check(self.folder, self.settings)

    def slices_with(self, n: int, **changes) -> dict:
        data = json.loads(json.dumps(SLICES))
        data["slices"][n - 1].update(changes)
        return data


class PromptTest(SpecTestCase):
    def test_names_the_skill_paths_gates_and_commands(self):
        worktree = self.root / "wt"
        text = spec.prompt(self.run.path, worktree, self.settings)
        self.assertIn(f"Follow the placido spec skill in {spec.SKILL}.", text)
        self.assertTrue(spec.SKILL.is_file())
        self.assertIn(f"**Issue:** {self.run.path / 'issue.md'} (from /repo/issues/02-many-names.md)", text)
        self.assertIn(f"{worktree}, on branch placido/02-many-names. Do not change it.", text)
        self.assertIn(str(self.folder / "agreement.md"), text)
        self.assertIn(str(self.folder / "slices.json"), text)
        self.assertIn("**Project notes for this role:** none", text)
        self.assertIn("**Slice gates:** every slice runs unit;", text)
        self.assertIn("- **data**, `y`", text)

    def test_role_notes_when_present(self):
        worktree = self.root / "wt"
        (worktree / ".placido" / "notes").mkdir(parents=True)
        (worktree / ".placido" / "notes" / "spec.md").write_text("Ask about RFCs.")
        text = spec.prompt(self.run.path, worktree, self.settings)
        self.assertIn(f"**Project notes for this role:** {worktree / '.placido/notes/spec.md'}", text)


class CheckTest(SpecTestCase):
    def test_good_output_passes(self):
        self.assertEqual(self.problems(), [])

    def test_missing_files(self):
        problems = spec.check(self.folder, self.settings)
        self.assertTrue(any("agreement.md is missing" in p for p in problems))
        self.assertTrue(any("slices.json is missing" in p for p in problems))

    def test_missing_section(self):
        problems = self.problems(agreement=AGREEMENT.replace("## Seams", "## Boundaries"))
        self.assertEqual(problems, ["agreement.md has no '## Seams' section."])

    def test_bad_json_and_empty_list(self):
        self.assertIn("not valid JSON", self.problems(slices="{nope")[0])
        self.assertIn('non-empty "slices" list', self.problems(slices={"slices": []})[0])

    def test_ids_must_count_up(self):
        problems = self.problems(slices=self.slices_with(2, id=5))
        self.assertEqual(problems, ["slice 2 in slices.json has id 5; ids count up from 1, so it should be 2."])

    def test_blocked_only_by_earlier_slices(self):
        problems = self.problems(slices=self.slices_with(1, blocked_by=[2]))
        self.assertIn("slice 1 in slices.json is blocked by 2; a slice can only be blocked by an earlier one.", problems)

    def test_gates_must_be_project_commands(self):
        problems = self.problems(slices=self.slices_with(2, gates=["e2e"]))
        self.assertEqual(
            problems, ["slice 2 in slices.json has gate 'e2e', which is not a project command (commands: unit, data)."]
        )

    def test_fields_are_required(self):
        problems = self.problems(slices=self.slices_with(1, delivers="", blocked_by="none", gates=None))
        self.assertIn("slice 1 in slices.json needs a non-empty 'delivers'.", problems)
        self.assertIn("slice 1 in slices.json needs 'blocked_by' as a list of slice ids.", problems)
        self.assertIn("slice 1 in slices.json needs 'gates' as a list of command names.", problems)

    def test_formatting_alone_is_no_mismatch(self):
        agreement = AGREEMENT.replace("**Accept several names**", "**Accept `several`   names**")
        self.assertEqual(self.problems(agreement=agreement), [])

    def test_slices_must_match_the_agreement(self):
        problems = self.problems(slices=self.slices_with(2, title="Refuse blanks"))
        self.assertEqual(problems, ["slice 2's title 'Refuse blanks' is not in agreement.md's Slices section."])


class SealTest(SpecTestCase):
    def test_seal_records_hashes_and_detects_changes(self):
        self.write()
        self.assertFalse(spec.sealed(self.folder))
        record = spec.seal(self.folder, self.run)
        self.assertTrue(spec.sealed(self.folder))
        self.assertEqual(set(record["files"]), {"agreement.md", "slices.json"})
        self.assertEqual(record["run"], self.run.path.name)
        self.assertIn("spec.sealed", [e["event"] for e in runlog.read_events(self.run.path)])
        (self.folder / "agreement.md").write_text(AGREEMENT + "\nA late edit.\n")
        self.assertFalse(spec.sealed(self.folder))

    def test_load_slices(self):
        self.write()
        self.assertEqual([s["title"] for s in spec.load_slices(self.folder)], ["Accept several names", "Reject empty names"])


class SpecCommandTest(SpecTestCase):
    def test_refuses_an_already_sealed_agreement(self):
        repo = self.root / "repo"
        repo.mkdir()
        import subprocess

        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        run = runlog.Run.create(self.root / "runs", "repo", "02-x", {})
        run.event("worktree.created", path=str(repo), workspace="w1")
        run.event("run.ready")
        folder = spec.issue_dir(run.path)
        (folder / "agreement.md").write_text(AGREEMENT)
        (folder / "slices.json").write_text(json.dumps(SLICES))
        spec.seal(folder, run)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"PLACIDO_RUNS_DIR": str(self.root / "runs")}), \
                mock.patch.object(Path, "cwd", return_value=repo), \
                mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
            code = cli.main(["spec"])
        self.assertEqual(code, 1)
        self.assertIn("already sealed", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class ThenRunTest(unittest.TestCase):
    """[spec] then_run in the user's config goes on with placido run once sealed."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.user = Path(tmp.name) / "user.toml"
        env = mock.patch.dict(os.environ, {"PLACIDO_USER_CONFIG": str(self.user)})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def after(self, **spec_args) -> int:
        args = argparse.Namespace(run=None, agent="codex", model="m", effort="low", **spec_args)
        with redirect_stdout(io.StringIO()):
            return cli._after_seal(args, lambda run_args: self.calls.append(run_args) or 7)

    def test_off_by_default(self):
        self.assertEqual(self.after(), 0)
        self.assertEqual(self.calls, [])

    def test_on_runs_with_the_run_s_own_agents(self):
        self.user.write_text("[spec]\nthen_run = true\n")
        self.assertEqual(self.after(), 7)  # the run's exit code
        (run_args,) = self.calls
        self.assertEqual((run_args.agent, run_args.model, run_args.effort), (None, None, None))

    def test_a_typo_is_an_error_named(self):
        self.user.write_text("[spec]\nthen_runs = true\n")
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self.after(), 0)
        self.assertIn("spec.then_runs: unknown key", err.getvalue())
