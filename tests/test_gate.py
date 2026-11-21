import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, config, gate, implement, runlog

CONFIG = (
    '[commands.unit]\nrun = "echo unit ran; test -f feature.txt"\n'
    '[commands.data]\nrun = "echo data ran on"\n'
    '[commands.touch]\nrun = "echo more >> feature.txt"\n'
)


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class GateTestCase(unittest.TestCase):
    """A real repository with an uncommitted change, and a run folder outside it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        (self.repo / ".placido").mkdir(parents=True)
        (self.repo / ".placido" / "config.toml").write_text(CONFIG)
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "user.email", "t@t")
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "README").write_text("hi\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        (self.repo / "feature.txt").write_text("done\n")
        self.settings = config.load(self.repo / ".placido" / "config.toml")
        self.run = runlog.Run.create(self.tmp / "runs", "repo", "02-x", {})
        self.step = self.run.step_dir(1, "implement-slice-1")

    def gates(self, *names: str, step: Path | None = None) -> list[str]:
        return implement.run_gates(list(names), self.settings, self.repo, step or self.step, {}, self.run)

    def events(self, kind: str) -> list[dict]:
        return [e for e in runlog.read_events(self.run.path) if e["event"] == kind]


class FlakyTest(unittest.TestCase):
    """The FLAKY lines a gate prints when tests passed only on its rerun of them."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "e2e.log"

    def names(self, text: str) -> list[str] | None:
        self.log.write_text(text)
        return gate.flaky(self.log)

    def test_goiabada_s_e2e_gate_a_heading_then_the_names_indented(self):
        self.assertEqual(self.names(
            "  2 failed\nRerunning these tests once, after the setup project's sweep:\n"
            "  [webkit] › tests/a.spec.ts:1:1 › one\n  [chromium] › tests/b.spec.ts:9:3 › two\n"
            "  2 passed (41.2s)\n\n"
            "FLAKY: these tests did not pass on the first run and passed on the rerun:\n"
            "  [webkit] › tests/a.spec.ts:1:1 › one\n  [chromium] › tests/b.spec.ts:9:3 › two\n"),
            ["[webkit] › tests/a.spec.ts:1:1 › one", "[chromium] › tests/b.spec.ts:9:3 › two"])

    def test_the_names_on_the_line_itself(self):
        self.assertEqual(self.names("ok\nFLAKY: test_login_retries\ndone\n"), ["test_login_retries"])
        self.assertEqual(self.names("FLAKY test_a, test_b\n"), ["test_a, test_b"])

    def test_bulleted_lines_colours_and_repeats(self):
        self.assertEqual(self.names(
            "\x1b[33mFLAKY\x1b[0m:\n- test_a\n* test_b\n\nnot a name\nFLAKY:\n  1. test_a\n  2) test_c\nRan 3\n"),
            ["test_a", "test_b", "test_c"])

    def test_a_flaky_line_that_names_nothing(self):
        self.assertEqual(self.names("FLAKY: some tests passed only on a rerun:\n\nsee above\n"), [])

    def test_no_flaky_line(self):
        self.assertIsNone(self.names("968 passed\nFLAKYNESS is low\n  FLAKY_TESTS=0\n"))
        self.assertIsNone(gate.flaky(self.log.with_name("missing.log")))


class FingerprintTest(GateTestCase):
    def test_the_same_files_give_the_same_fingerprint(self):
        self.assertEqual(gate.fingerprint(self.repo), gate.fingerprint(self.repo))

    def test_a_changed_or_new_file_changes_it_and_an_ignored_one_does_not(self):
        first = gate.fingerprint(self.repo)
        (self.repo / "build").mkdir()
        (self.repo / "build" / "out.bin").write_text("binary\n")
        self.assertEqual(gate.fingerprint(self.repo), first)
        (self.repo / "notes.txt").write_text("new\n")
        second = gate.fingerprint(self.repo)
        self.assertNotEqual(second, first)
        (self.repo / "feature.txt").write_text("changed\n")
        self.assertNotEqual(gate.fingerprint(self.repo), second)

    def test_it_is_the_tree_the_commit_gets_and_leaves_the_index_alone(self):
        before = sh(self.repo, "git", "status", "--porcelain")
        tree = gate.fingerprint(self.repo)
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), before)  # feature.txt still untracked
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "slice")
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD^{tree}"), tree)

    def test_none_outside_a_repository(self):
        self.assertIsNone(gate.fingerprint(self.tmp))


class RunGatesTest(GateTestCase):
    def test_a_gate_that_passed_on_the_same_files_is_not_run_again(self):
        self.assertEqual(self.gates("unit", "data mysql"), [])
        later = self.run.step_dir(2, "final-gates")
        self.assertEqual(self.gates("unit", "data mysql", step=later), [])
        self.assertEqual([e["gate"] for e in self.events("gate.start")], ["unit", "data mysql"])
        reused = self.events("gate.reused")
        self.assertEqual([(e["gate"], e["by"]) for e in reused], [("unit", "placido"), ("data mysql", "placido")])
        self.assertEqual(reused[0]["log"], str(self.step / "gates" / "unit.log"))
        self.assertIn(f"its output is in {self.step / 'gates' / 'unit.log'}",
                      (later / "gates" / "unit.log").read_text())

    def test_a_change_to_the_files_runs_it_again(self):
        self.gates("unit")
        (self.repo / "feature.txt").write_text("fixed\n")
        self.gates("unit")
        self.assertEqual(len(self.events("gate.start")), 2)
        self.assertEqual(self.events("gate.reused"), [])

    def test_another_argument_is_another_gate(self):
        self.gates("data mysql")
        self.gates("data postgres")
        self.assertEqual([e["gate"] for e in self.events("gate.start")], ["data mysql", "data postgres"])

    def test_a_failure_is_not_recorded(self):
        (self.repo / "feature.txt").unlink()
        (problem,) = self.gates("unit")
        self.assertIn("gate unit", problem)
        self.assertFalse((self.run.path / gate.RECORD).exists())
        self.gates("unit")
        self.assertEqual(len(self.events("gate.start")), 2)

    def test_a_pass_that_changed_the_files_is_not_recorded(self):
        self.assertEqual(self.gates("touch"), [])
        self.assertFalse((self.run.path / gate.RECORD).exists())

    def test_a_pass_of_another_gate_with_the_same_command_does_not_count(self):
        self.settings = config.load(self.write_config(CONFIG + '[commands.lint]\nrun = "echo data ran on"\n'))
        self.gates("data")
        self.gates("lint")
        self.assertEqual([e["gate"] for e in self.events("gate.start")], ["data", "lint"])

    def write_config(self, text: str) -> Path:
        path = self.tmp / "other.toml"
        path.write_text(text)
        return path


class CommandTest(GateTestCase):
    def gate_cli(self, *words: str, env: dict[str, str] | None = None) -> tuple[int, str]:
        env = {"PLACIDO_STEP_DIR": str(self.step), "PLACIDO_RUN_DIR": str(self.run.path),
               "PLACIDO_REPO": str(self.repo), "PLACIDO_WORKTREE": str(self.repo)} if env is None else env
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            code = cli.main(["gate", *words])
        return code, out.getvalue()

    def test_an_agent_s_pass_spares_placido_the_run(self):
        code, said = self.gate_cli("data", "mysql")
        self.assertEqual(code, 0)
        self.assertIn("data ran on mysql", said)
        self.assertIn("gate data mysql passed", said)
        (entry,) = [json.loads(line) for line in (self.run.path / gate.RECORD).read_text().splitlines()]
        self.assertEqual((entry["gate"], entry["by"], entry["log"]),
                         ("data mysql", "agent", str(self.step / "gates" / "data-mysql.log")))
        self.assertEqual(self.gates("data mysql"), [])
        self.assertEqual(self.events("gate.start"), [])
        self.assertEqual(self.events("gate.reused")[0]["by"], "agent")
        self.assertEqual((self.step / "gates" / "data-mysql.log").read_text(), "data ran on mysql\n")

    def test_a_step_without_the_run_variable_finds_its_run_from_its_folder(self):
        code, _ = self.gate_cli("data", "mysql", env={"PLACIDO_STEP_DIR": str(self.step),
                                                      "PLACIDO_REPO": str(self.repo), "PLACIDO_WORKTREE": str(self.repo)})
        self.assertEqual(code, 0)
        self.assertTrue((self.run.path / gate.RECORD).is_file())

    def test_an_option_in_the_gate_reaches_its_command(self):
        code, said = self.gate_cli("data", "--race")
        self.assertEqual(code, 0)
        self.assertIn("data ran on --race", said)

    def test_a_failure_says_so_with_its_exit(self):
        (self.repo / "feature.txt").unlink()
        code, said = self.gate_cli("unit")
        self.assertEqual(code, 1)
        self.assertIn("gate unit FAILED with exit 1", said)

    def test_refusals(self):
        self.assertEqual(self.gate_cli()[0], 2)
        code, said = self.gate_cli("nosuch")
        self.assertEqual(code, 2)
        self.assertIn("'nosuch' is not a project command", said)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLACIDO_STEP_DIR", None)
            os.environ.pop("PLACIDO_RUN_DIR", None)
            code, said = self.gate_cli("unit", env={})
        self.assertEqual(code, 2)
        self.assertIn("run it from a placido step", said)


if __name__ == "__main__":
    unittest.main()
