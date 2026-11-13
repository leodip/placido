import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, mutate

CODE = "def double(n):\n    return n * 2\n"
TEST = "from code import double\nassert double(3) == 6\n"


def _running(pid: int) -> bool:
    """Whether the process runs; a zombie waiting to be reaped does not."""

    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class MutateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.code = self.dir / "code.py"
        self.code.write_text(CODE)
        (self.dir / "check.py").write_text(TEST)
        self.test = [f"cd {self.dir} && python3 check.py"]


class MutateTest(MutateTestCase):
    def test_killed_when_the_tests_fail(self):
        result = mutate.mutate(self.code, "n * 2", "n * 3", self.test)
        self.assertEqual(result.outcome, mutate.KILLED)
        self.assertNotEqual(result.exit, 0)
        self.assertEqual(self.code.read_text(), CODE)

    def test_survived_when_the_tests_still_pass(self):
        result = mutate.mutate(self.code, "n * 2", "n + n", self.test)
        self.assertEqual(result.outcome, mutate.SURVIVED)
        self.assertEqual(self.code.read_text(), CODE)

    def test_the_mutation_is_in_place_while_the_tests_run(self):
        result = mutate.mutate(self.code, "n * 2", "n * 7", [f"cat {self.code}"])
        self.assertIn("n * 7", result.output)

    def test_restored_even_when_the_command_cannot_run(self):
        result = mutate.mutate(self.code, "n * 2", "n * 3", ["/no/such/test-runner"])
        self.assertEqual(result.outcome, mutate.KILLED)  # the shell reports failure
        self.assertEqual(self.code.read_text(), CODE)

    def test_restored_on_an_exception(self):
        with mock.patch("subprocess.Popen", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                mutate.mutate(self.code, "n * 2", "n * 3", self.test)
        self.assertEqual(self.code.read_text(), CODE)

    def test_timeout(self):
        result = mutate.mutate(self.code, "n * 2", "n * 3", ["sleep 5"], timeout=0.2)
        self.assertEqual(result.outcome, mutate.TIMEOUT)
        self.assertEqual(self.code.read_text(), CODE)

    def test_a_timeout_stops_what_the_tests_started(self):
        """A test binary looping in the mutant ran on after its mutation timed out."""

        pidfile = self.dir / "child.pid"
        result = mutate.mutate(
            self.code, "n * 2", "n * 3", [f"echo started; sleep 30 & echo $! > {pidfile}; wait"], timeout=0.5,
        )
        child = int(pidfile.read_text())
        self.addCleanup(lambda: _kill_quietly(child))
        self.assertEqual(result.outcome, mutate.TIMEOUT)
        self.assertIn("started", result.output)
        self.assertLess(result.seconds, 5)
        for _ in range(50):
            if not _running(child):
                break
            time.sleep(0.05)
        self.assertFalse(_running(child))

    def test_output_that_is_not_utf8_still_gets_an_outcome(self):
        result = mutate.mutate(self.code, "n * 2", "n * 3", ["printf 'body: \\037\\213\\010'; exit 1"])
        self.assertEqual(result.outcome, mutate.KILLED)
        self.assertEqual(result.output, "body: \x1f\ufffd\x08")
        self.assertEqual(self.code.read_text(), CODE)

    def test_text_must_match_exactly_once(self):
        with self.assertRaisesRegex(mutate.MutateError, "not found"):
            mutate.mutate(self.code, "n * 9", "x", self.test)
        self.code.write_text("a = 1\nb = 1\n")
        with self.assertRaisesRegex(mutate.MutateError, "found 2 times"):
            mutate.mutate(self.code, "= 1", "= 2", self.test)

    def test_no_op_mutation_is_refused(self):
        with self.assertRaisesRegex(mutate.MutateError, "same as the original"):
            mutate.mutate(self.code, "n * 2", "n * 2", self.test)


class CommandTest(MutateTestCase):
    def placido(self, *argv: str, step_dir: Path | None = None) -> tuple[int, str]:
        out = io.StringIO()
        env = {"PLACIDO_STEP_DIR": str(step_dir)} if step_dir else {}
        with redirect_stdout(out), mock.patch.dict(os.environ, env, clear=False):
            if not step_dir:
                os.environ.pop("PLACIDO_STEP_DIR", None)
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_records_in_the_step_folder(self):
        step_dir = self.dir / "step"
        code, out = self.placido(
            "mutate", str(self.code), "--replace", "n * 2", "--with", "n * 3", "--", *self.test,
            step_dir=step_dir,
        )
        self.assertEqual(code, 0)
        self.assertIn("killed:", out)
        (entry,) = mutate.read(step_dir)
        self.assertEqual(entry["outcome"], "killed")
        self.assertEqual(entry["replace"], "n * 2")
        self.assertEqual(entry["with"], "n * 3")
        self.assertTrue((step_dir / "mutations" / entry["log"]).is_file())

    def test_records_output_that_is_not_utf8(self):
        step_dir = self.dir / "step"
        code, out = self.placido(
            "mutate", str(self.code), "--replace", "n * 2", "--with", "n * 3",
            "--", "printf '\\037\\213\\010' >&2; exit 1",
            step_dir=step_dir,
        )
        self.assertEqual(code, 0)
        self.assertIn("killed:", out)
        (entry,) = mutate.read(step_dir)
        self.assertEqual(entry["outcome"], "killed")
        log = step_dir / "mutations" / entry["log"]
        self.assertEqual(log.read_text(encoding="utf-8"), "\x1f\ufffd\x08")

    def test_survivor_exits_one(self):
        code, out = self.placido(
            "mutate", str(self.code), "--replace", "n * 2", "--with", "n + n", "--", *self.test
        )
        self.assertEqual(code, 1)
        self.assertIn("SURVIVED", out)

    def test_command_split_at_double_dash_keeps_test_flags(self):
        code, out = self.placido(
            "mutate", str(self.code), "--replace", "n * 2", "--with", "n * 3",
            "--", "python3", "-c", f"import sys; sys.path.insert(0, '{self.dir}'); from code import double; assert double(3) == 6",
        )
        self.assertEqual(code, 0)

    def test_missing_command_and_bad_text(self):
        code, out = self.placido("mutate", str(self.code), "--replace", "n * 2", "--with", "n * 3")
        self.assertEqual(code, 2)
        self.assertIn("give the test command", out)
        code, out = self.placido("mutate", str(self.code), "--replace", "zzz", "--with", "y", "--", "true")
        self.assertEqual(code, 2)
        self.assertIn("not found", out)


if __name__ == "__main__":
    unittest.main()
