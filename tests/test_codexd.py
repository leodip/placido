import os
import tempfile
import unittest
from pathlib import Path

from placido import cli, close, codexd, config, doctor, runlog
from placido.herdr import Herdr
from placido.proc import Result
from tests.test_start import StartTestCase, make_repo

DAEMON = ["/home/u/.codex/packages/app-server-daemon/releases/0.161.0/bin/codex", "app-server",
          "--listen", "unix://", "--managed-daemon"]
LOOP = ["/home/u/.codex/packages/app-server-daemon/releases/0.161.0/bin/codex", "app-server", "daemon",
        "pid-update-loop"]
HOST = ["/home/u/.codex/packages/app-server-daemon/releases/0.161.0/bin/codex-code-mode-host"]


class FakeProc:
    """A /proc of a few processes: each its arguments and its folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()
        self.next = 100

    def add(self, args: list[str], folder: str) -> int:
        self.next += 1
        entry = self.root / str(self.next)
        entry.mkdir()
        (entry / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in args))
        os.symlink(folder, entry / "cwd")
        return self.next


class Case(unittest.TestCase):
    def use_fake_proc(self, tmp: Path) -> None:
        self.proc = FakeProc(tmp / "proc")
        self.commands: list[tuple[list[str], Path]] = []
        self.answer = Result(0, "")
        saved = codexd.PROC[0], codexd.RUNNER[0]
        codexd.PROC[0] = self.proc.root

        def runner(argv: list[str], cwd: Path) -> Result:
            self.commands.append((argv, cwd))
            return self.answer

        codexd.RUNNER[0] = runner
        self.addCleanup(lambda: (codexd.PROC.__setitem__(0, saved[0]), codexd.RUNNER.__setitem__(0, saved[1])))

    def daemon_commands(self) -> list[str]:
        return [argv[3] for argv, cwd in self.commands if cwd == Path.home()]


class FindTest(Case):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.use_fake_proc(self.tmp)
        self.run = runlog.Run.create(self.tmp / "runs", "proj", "01-x", {})

    def events(self, kind: str) -> list[dict]:
        return [e for e in runlog.read_events(self.run.path) if e["event"] == kind]

    def test_finds_the_daemon_and_whether_its_folder_is_gone(self):
        self.proc.add(LOOP, str(self.tmp))
        self.proc.add(HOST, "/w/placido-500 (deleted)")
        pid = self.proc.add(DAEMON, "/w/placido-500 (deleted)")
        self.assertEqual(codexd.daemons(), [codexd.Daemon(pid, Path("/w/placido-500"), True)])

    def test_a_folder_that_is_there_is_not_gone(self):
        self.proc.add(DAEMON, str(self.tmp))
        self.assertFalse(codexd.daemons()[0].gone)
        self.assertIsNone(codexd.problem())

    def test_sessions_are_codex_processes_other_than_the_daemon(self):
        self.proc.add(DAEMON, str(self.tmp))
        self.proc.add(HOST, str(self.tmp))
        session = self.proc.add(["codex", "--yolo"], str(self.tmp))
        node = self.proc.add(["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js", "resume"], str(self.tmp))
        self.proc.add([], str(self.tmp))  # a zombie has no arguments
        self.proc.add(["bash"], str(self.tmp))
        self.assertEqual(sorted(codexd.sessions()), [session, node])

    def test_without_a_daemon_it_is_started_from_home(self):
        codexd.ensure(self.run)
        self.assertEqual(self.daemon_commands(), ["start"])

    def test_a_healthy_daemon_is_left_alone(self):
        self.proc.add(DAEMON, str(self.tmp))
        codexd.ensure(self.run)
        self.assertEqual(self.commands, [])

    def test_a_daemon_whose_folder_is_gone_is_restarted_from_home(self):
        self.proc.add(DAEMON, "/w/placido-500 (deleted)")
        codexd.ensure(self.run)
        self.assertEqual(self.daemon_commands(), ["restart"])
        self.assertEqual(self.events("codex.daemon_restarted")[0]["was"], "/w/placido-500")

    def test_a_session_on_it_keeps_it_running(self):
        self.proc.add(DAEMON, "/w/placido-500 (deleted)")
        self.proc.add(["codex", "--yolo"], str(self.tmp))
        codexd.ensure(self.run)
        self.assertEqual(self.commands, [])
        kept = self.events("codex.daemon_kept")[0]
        self.assertEqual((kept["sessions"], kept["fix"]), (1, codexd.FIX))
        self.assertIn(codexd.FIX, codexd.problem())

    def test_a_failed_restart_is_a_warning(self):
        self.proc.add(DAEMON, "/w/placido-500 (deleted)")
        self.answer = Result(1, "no socket")
        codexd.ensure(self.run)
        self.assertEqual(self.events("codex.daemon_warning")[0]["error"], "no socket")

    def test_the_doctor_reports_a_daemon_whose_folder_is_gone(self):
        self.assertEqual(doctor.check_codex_daemon().status, doctor.OK)
        self.proc.add(DAEMON, "/w/placido-500 (deleted)")
        check = doctor.check_codex_daemon()
        self.assertEqual(check.status, doctor.FAIL)
        self.assertIn("/w/placido-500", check.detail)


class CloseTest(StartTestCase, Case):
    """Closing moves Codex's daemon out of the worktree it is about to remove."""

    def setUp(self) -> None:
        super().setUp()
        self.use_fake_proc(self.tmp)
        self.repo = make_repo(self.tmp, setup="true")
        self.run = self.start("02", self.repo)
        self.worktree = self.tmp / "worktrees" / "placido-02-many-names"

    def close(self) -> None:
        cfg = self.tmp / "close.toml"
        cfg.write_text("[project]\n")
        close.close(self.run, Herdr(self.fake), config.load(cfg))

    def test_a_daemon_in_the_worktree_is_restarted_before_it_goes(self):
        (self.worktree / "sub").mkdir()
        self.proc.add(DAEMON, str(self.worktree / "sub"))
        self.close()
        self.assertEqual(self.daemon_commands(), ["restart"])
        events = self.events(self.run.path)
        self.assertLess(events.index("codex.daemon_restarted"), events.index("worktree.removed"))
        self.assertEqual(cli._codex_daemon_lines(self.run.path),
                         ["Codex's daemon ran from the worktree; it is restarted from your home folder."])

    def test_with_a_session_on_it_close_says_how_to_restart_it_later(self):
        self.proc.add(DAEMON, str(self.worktree))
        self.proc.add(["codex", "--yolo"], str(self.tmp))
        self.close()
        self.assertEqual(self.commands, [])
        [line] = cli._codex_daemon_lines(self.run.path)
        self.assertIn("1 Codex session(s) still use it", line)
        self.assertTrue(line.endswith(codexd.FIX))

    def test_a_daemon_elsewhere_is_left_alone(self):
        self.proc.add(DAEMON, str(self.tmp))
        self.run.event("codex.daemon_kept", sessions=1, fix=codexd.FIX)  # from the run, before closing
        self.close()
        self.assertEqual(self.commands, [])
        self.assertEqual(cli._codex_daemon_lines(self.run.path), [])
