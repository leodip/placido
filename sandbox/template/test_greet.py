import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from greet import greet, main


class GreetTest(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")

    def test_main_prints_the_greeting(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["Ada"]), 0)
        self.assertEqual(out.getvalue(), "Hello, Ada!\n")

    def test_main_needs_one_name(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main([]), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue().startswith("usage: greet.py "), err.getvalue())


if __name__ == "__main__":
    unittest.main()
