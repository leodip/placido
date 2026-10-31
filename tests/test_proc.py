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
        code = proc.run_shell('sleep 0.35; echo "$WHO"; exit 3', self.tmp, {"WHO": "me"}, self.tmp / "x.log",
                              lambda s, line: said.append(s))
        self.assertEqual(code, 3)
        self.assertEqual((self.tmp / "x.log").read_text(), "me\n")
        self.assertGreaterEqual(len(said), 2)
        self.assertEqual(said, sorted(said))

    def test_a_quick_command_says_nothing(self):
        said: list[float] = []
        self.assertEqual(proc.run_shell("true", self.tmp, {}, self.tmp / "x.log", lambda s, line: said.append(s)), 0)
        self.assertEqual(said, [])

    def test_an_interrupted_wait_leaves_no_command_behind(self):
        def interrupt(seconds: float, line: str) -> None:
            raise KeyboardInterrupt

        pid_file = self.tmp / "pid"
        began = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            proc.run_shell(f"echo $$ > {pid_file}; exec sleep 30", self.tmp, {}, self.tmp / "x.log", interrupt)
        self.assertLess(time.monotonic() - began, 10)  # killed, not waited out
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_says_what_it_last_printed(self):
        # 2026-10-31: two runs at a shared e2e gate both read "running"; the one waiting
        # on the other's lock now says so.
        lines: list[str] = []
        command = "echo starting; printf 'waiting for it.\\n\\n'; sleep 0.35"
        proc.run_shell(command, self.tmp, {}, self.tmp / "x.log", lambda s, line: lines.append(line))
        self.assertTrue(lines)
        self.assertEqual(lines[-1], "waiting for it.")

    def test_the_last_line_is_plain_and_short(self):
        log = self.tmp / "x.log"
        self.assertEqual(proc.last_line(log), "")  # no log yet
        log.write_text("first\n\x1b[32m  ✓ 41   a\ttest \x1b[0m\n\n")
        self.assertEqual(proc.last_line(log), "✓ 41 a test")
        log.write_text("progress 10%\rprogress 90%")
        self.assertEqual(proc.last_line(log), "progress 90%")
        log.write_text("x" * 500)
        self.assertEqual(len(proc.last_line(log)), proc.SAID_CHARS)
        self.assertTrue(proc.last_line(log).endswith("…"))
        log.write_text("")
        self.assertEqual(proc.last_line(log), "")

    def test_setup_says_it_still_runs(self):
        run = runlog.Run.create(self.tmp / "runs", "p", "1-x", {}, echo=io.StringIO())
        self.assertEqual(start.run_project_command(run, "setup", "echo building; sleep 0.25", self.tmp, {}), 0)
        self.assertIn("setup running (0:00): building", run.echo.getvalue())
        self.assertNotIn("running (", run.events_path.read_text())


if __name__ == "__main__":
    unittest.main()
