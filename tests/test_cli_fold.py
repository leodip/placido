import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, driver, runlog

ITEMS = [{"id": "F1", "title": "one", "why": "outside"}, {"id": "F2", "title": "two", "why": "outside"},
         {"id": "R1-3", "title": "three", "description": "deferred"}]


class AskFoldTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = runlog.Run.create(Path(tmp.name), "proj", "01-x", {})
        self.ctx = driver.Context(self.run, None, None, None, None, Path(tmp.name), {})
        for name in ("notify", "show"):
            patch = mock.patch.object(driver, name)
            patch.start()
            self.addCleanup(patch.stop)

    def ask(self, *answers: str) -> tuple[list[int], str]:
        replies = iter(answers)
        out = io.StringIO()
        with redirect_stdout(out):
            picks = cli._ask_fold(self.ctx, ITEMS, reply=lambda prompt: next(replies))
        return picks, out.getvalue()

    def test_lists_each_with_why(self):
        _, shown = self.ask("")
        self.assertIn("  1. one\n     outside\n", shown)
        self.assertIn("  3. three\n     deferred\n", shown)

    def test_answers(self):
        self.assertEqual(self.ask("")[0], [])
        self.assertEqual(self.ask("none")[0], [])
        self.assertEqual(self.ask("all")[0], [0, 1, 2])
        self.assertEqual(self.ask("3, 1")[0], [0, 2])
        self.assertEqual(runlog.last_event(self.run.path, "review.fold_chosen")["items"], ["F1", "R1-3"])

    def test_asks_again_after_a_wrong_answer(self):
        picks, shown = self.ask("7", "x", "2")
        self.assertEqual(picks, [1])
        self.assertEqual(shown.count("Choose from 1 to 3."), 2)

    def test_without_a_terminal_nothing_is_folded(self):
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertEqual(cli._ask_fold(self.ctx, ITEMS), [])
