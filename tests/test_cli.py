import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, doctor, runlog
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

    def test_runs_shows_a_deferred_run_as_deferred(self):
        run = runlog.Run.create(self.root, "goiabada", "494-drop", {})
        run.event("run.end", outcome="deferred", reason="start once tool 2.16.0 is out")
        code, out, _ = self.placido("runs")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), [f"goiabada/494-drop/{run.path.name}", "deferred"])

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


class PRCommandTest(unittest.TestCase):
    def test_reads_a_number_a_hash_or_a_url(self):
        for text in ("602", "#602", "https://github.com/leodip/goiabada/pull/602",
                     "https://github.com/leodip/goiabada/pull/602/files"):
            self.assertEqual(cli.pr_number(text), 602, text)
        for text in ("abc", "https://github.com/leodip/goiabada/issues/602", ""):
            self.assertIsNone(cli.pr_number(text), text)

    def test_refuses_what_is_not_a_pull_request(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["pr", "nope"]), 1)
        self.assertIn("'nope' is not a pull request number or URL", err.getvalue())

    def test_no_menu_option(self):
        args = cli.build_parser().parse_args(["pr", "602", "--no-menu"])
        self.assertEqual((args.pr, args.no_menu, args.func), ("602", True, cli.cmd_pr))


class RetroCommandTest(unittest.TestCase):
    """placido retro on a closed run, by default or by issue, and on the last few together."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = Path(tmp.name) / "runs"
        self.repo = Path(tmp.name) / "proj"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        here = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, here)
        patch = mock.patch.dict(os.environ, {"PLACIDO_RUNS_DIR": str(self.runs), "HERDR_PANE_ID": "w7:p1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.older = self.made("01-a", 1000)
        self.newer = self.made("02-b", 2000)
        self.calls = []

    def made(self, issue: str, age: int) -> Path:
        path = self.runs / "proj" / issue / "2026-01-01T000000"
        path.mkdir(parents=True)
        (path / "run.json").write_text(json.dumps({"project": "proj", "issue": issue, "repo": str(self.repo)}))
        events = ("run.ready", "run.end", "run.closed")
        (path / "events.jsonl").write_text("".join(json.dumps({"event": e}) + "\n" for e in events))
        os.utime(path / "run.json", (age, age))
        return path

    def retro(self, runner, agent, runs, code):
        from placido.step import StepResult

        self.calls.append((runner, runs, code))
        (runner.run.path / "retro.md").write_text("# Retro\n\n## Suggestions\n\nNone.\n")
        return StepResult("success", runner.run.path)

    def placido(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err), \
                mock.patch.object(cli.retrospect, "retro", side_effect=self.retro), mock.patch.object(cli, "Herdr"):
            code = cli.main(["retro", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_with_every_run_closed_the_newest(self):
        code, out, _ = self.placido()
        self.assertEqual(code, 0)
        ((runner, runs, code_dir),) = self.calls
        self.assertEqual((runs, runner.run.path, code_dir), ([self.newer], self.newer, self.repo.resolve()))
        self.assertEqual(runner.workspace_id, "w7")
        self.assertIn("placido: a retro on 02-b, its run of 2026-01-01T000000", out)

    def test_a_retro_of_one_run_shows_no_status_in_it_and_of_several_does(self):
        self.placido()
        self.placido("--last", "2")
        self.assertEqual([runner.show_status for runner, _, _ in self.calls], [False, True])

    def test_an_issue_by_prefix(self):
        self.assertEqual(self.placido("01")[0], 0)
        self.assertEqual(self.calls[0][1], [self.older])

    def test_the_last_runs_together_in_a_folder_of_their_own(self):
        code, out, _ = self.placido("--last", "5")
        self.assertEqual(code, 0)
        ((runner, runs, code_dir),) = self.calls
        self.assertEqual((runs, code_dir), ([self.newer, self.older], self.repo.resolve()))
        host = runner.run.path
        self.assertEqual(host.parent, self.runs / "proj" / "retros")
        self.assertEqual(runner.env["PLACIDO_RUN_DIR"], str(host))
        self.assertIn("placido: a retro on the last 2 runs of proj: 02-b, 01-a", out)
        self.assertIn(f"placido: kept in {host / 'retro.md'}", out)

    def test_last_names_no_issue_and_takes_at_least_one_run(self):
        for argv, error in ((("--last", "2", "01"), "name no issue or run with it"),
                            (("--last", "0"), "at least 1 run, not 0")):
            with self.subTest(argv=argv):
                code, _, err = self.placido(*argv)
                self.assertEqual(code, 1)
                self.assertIn(error, err)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.runs / "proj" / "retros").exists())

    def test_outside_herdr_several_runs_are_refused_before_any_folder_is_made(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": ""}):
            code, _, err = self.placido("--last", "2")
        self.assertEqual(code, 1)
        self.assertIn("a retro on several runs; run placido retro from a Herdr pane", err)
        self.assertFalse((self.runs / "proj" / "retros").exists())
