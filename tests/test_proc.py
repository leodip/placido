import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from placido import proc, runlog, start


class RunShellTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patch = mock.patch.object(proc, "PROGRESS_SECONDS", 0.1)
        patch.start()
        self.addCleanup(patch.stop)

    def test_says_it_still_runs_until_it_ends(self):
        said: list[float] = []
        code = proc.run_shell('sleep 0.35; echo "$WHO"; exit 3', self.tmp, {"WHO": "me"}, self.tmp / "x.log", said.append)
        self.assertEqual(code, 3)
        self.assertEqual((self.tmp / "x.log").read_text(), "me\n")
        self.assertGreaterEqual(len(said), 2)
        self.assertEqual(said, sorted(said))

    def test_a_quick_command_says_nothing(self):
        said: list[float] = []
        self.assertEqual(proc.run_shell("true", self.tmp, {}, self.tmp / "x.log", said.append), 0)
        self.assertEqual(said, [])

    def test_an_interrupted_wait_leaves_no_command_behind(self):
        def interrupt(seconds: float) -> None:
            raise KeyboardInterrupt

        pid_file = self.tmp / "pid"
        began = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            proc.run_shell(f"echo $$ > {pid_file}; exec sleep 30", self.tmp, {}, self.tmp / "x.log", interrupt)
        self.assertLess(time.monotonic() - began, 10)  # killed, not waited out
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_setup_says_it_still_runs(self):
        run = runlog.Run.create(self.tmp / "runs", "p", "1-x", {}, echo=io.StringIO())
        self.assertEqual(start.run_project_command(run, "setup", "sleep 0.25", self.tmp, {}), 0)
        self.assertIn("setup running (0:00)", run.echo.getvalue())
        self.assertNotIn("running (", run.events_path.read_text())


if __name__ == "__main__":
    unittest.main()
