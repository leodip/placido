import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, doctor
from placido.doctor import FAIL, OK, WARN

PLACIDO = Path(__file__).resolve().parent.parent / "bin" / "placido"


def doctor_with(checks: list[doctor.Check]) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch.object(doctor, "run_checks", return_value=checks), redirect_stdout(out):
        code = cli.main(["doctor"])
    return code, out.getvalue()


class DoctorCommandTest(unittest.TestCase):
    def test_all_green_exits_zero(self):
        code, out = doctor_with([doctor.Check("herdr", OK, "herdr 0.9.3")])
        self.assertEqual(code, 0)
        self.assertIn("✓ herdr  herdr 0.9.3", out)

    def test_a_warning_still_exits_zero(self):
        code, _ = doctor_with([doctor.Check("herdr pane", WARN, "not inside")])
        self.assertEqual(code, 0)

    def test_a_failure_exits_one(self):
        code, _ = doctor_with([doctor.Check("herdr", OK, ""), doctor.Check("billing", FAIL, "")])
        self.assertEqual(code, 1)


class RunCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patches = [
            mock.patch.dict(os.environ, {"PLACIDO_RUNS_DIR": tmp.name}),
            mock.patch.object(doctor, "tool_versions", return_value={"pi": "1.0.0"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def placido(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_trial_makes_a_run_folder_with_a_full_log(self):
        code, out, _ = self.placido("trial")
        self.assertEqual(code, 0)
        (run,) = self.root.glob("trial/00-trial/*")
        record = json.loads((run / "run.json").read_text())
        self.assertEqual(record["versions"], {"pi": "1.0.0"})
        self.assertTrue((run / "steps" / "01-trial-note" / "prompt.md").is_file())
        events = [json.loads(line)["event"] for line in (run / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["run.start", "step.start", "step.end", "run.end"])
        self.assertIn("step.end      step=01-trial-note outcome=success", out)
        self.assertIn(f"run folder: {run}", out)

    def test_log_prints_the_latest_run(self):
        self.placido("trial")
        code, out, _ = self.placido("log")
        self.assertEqual(code, 0)
        self.assertIn("run.end       outcome=success", out)

    def test_log_takes_a_run_folder(self):
        self.placido("trial")
        (run,) = self.root.glob("trial/00-trial/*")
        code, out, _ = self.placido("log", str(run))
        self.assertEqual(code, 0)
        self.assertIn(f"run folder: {run}", out)

    def test_runs_lists_newest_first_with_outcomes(self):
        self.placido("trial")
        (first,) = self.root.glob("trial/00-trial/*")
        os.utime(first / "run.json", (1, 1))
        self.placido("trial")
        unfinished = self.root / "other" / "01-x" / "r1"
        unfinished.mkdir(parents=True)
        (unfinished / "run.json").write_text("{}")
        os.utime(unfinished / "run.json", (2, 2))
        code, out, _ = self.placido("runs")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^trial/00-trial/\S+  success$")
        self.assertEqual(lines[1], "other/01-x/r1                       unfinished")
        self.assertTrue(lines[2].startswith(f"trial/00-trial/{first.name}"))

    def test_runs_limit(self):
        self.placido("trial")
        self.placido("trial")
        _, out, _ = self.placido("runs", "-n", "1")
        self.assertEqual(len(out.splitlines()), 1)

    def test_runs_when_empty(self):
        _, out, _ = self.placido("runs")
        self.assertIn("no runs under", out)

    def test_log_takes_a_name_as_runs_prints_it(self):
        self.placido("trial")
        (run,) = self.root.glob("trial/00-trial/*")
        code, out, _ = self.placido("log", f"trial/00-trial/{run.name}")
        self.assertEqual(code, 0)
        self.assertIn("run.end", out)

    def test_log_without_runs_fails(self):
        code, _, err = self.placido("log")
        self.assertEqual(code, 1)
        self.assertIn("no run found", err)

    def test_log_of_a_folder_that_is_not_a_run_fails(self):
        code, _, err = self.placido("log", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn(f"no run found at {self.root}", err)


class InterruptTest(unittest.TestCase):
    def test_ctrl_c_says_how_to_resume_instead_of_a_traceback(self):
        err = io.StringIO()
        with mock.patch.object(cli, "cmd_runs", side_effect=KeyboardInterrupt), \
                mock.patch("sys.stderr", err):
            code = cli.main(["runs"])
        self.assertEqual(code, 130)
        self.assertIn("interrupted. Run the same command again to resume.", err.getvalue())


class EntryPointTest(unittest.TestCase):
    def test_version_runs_through_the_script(self):
        done = subprocess.run([str(PLACIDO), "--version"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "placido 0.0.1")

    def test_no_command_prints_help(self):
        done = subprocess.run([str(PLACIDO)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("doctor", done.stdout)


if __name__ == "__main__":
    unittest.main()
