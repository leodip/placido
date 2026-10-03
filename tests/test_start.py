import os
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, doctor, runlog, start, status
from placido.herdr import Herdr, HerdrError, Worktree
from placido.proc import Result


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, setup: str = "touch setup-ran", base: str = "main") -> Path:
    repo = root / "proj"
    (repo / "issues").mkdir(parents=True)
    (repo / ".placido").mkdir()
    (repo / ".placido" / "config.toml").write_text(f'[project]\nbase = "{base}"\nsetup = "{setup}"\n')
    (repo / "issues" / "01-shout-flag.md").write_text("# Shout\n")
    (repo / "issues" / "02-many-names.md").write_text("# Many\n")
    (repo / ".gitignore").write_text("setup-ran\n")
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


class FakeHerdr:
    """Answers herdr commands like the real CLI; worktree create makes a real git worktree."""

    def __init__(self, worktrees: Path) -> None:
        self.worktrees = worktrees
        self.calls: list[list[str]] = []
        self.fail: dict[str, Result] = {}
        self.statuses: list[str] = []  # each status placido kept for the run, in order
        self.workspaces: list[dict] = []  # what `workspace list` answers

    def __call__(self, argv: list[str], timeout: float | None = None) -> Result:
        self.calls.append(argv)
        command = " ".join(argv[1:3])
        if command in self.fail:
            return self.fail[command]
        if command == "worktree create":
            opts = dict(zip(argv[3::2], argv[4::2]))
            path = self.worktrees / opts["--branch"].replace("/", "-")
            sh(Path(opts["--cwd"]), "git", "worktree", "add", "-q", "-b", opts["--branch"], str(path), opts["--base"])
            self.last_worktree = path
            return Result(0, json.dumps({"result": {
                "worktree": {"path": str(path), "branch": opts["--branch"]},
                "workspace": {"workspace_id": "w2"},
                "root_pane": {"pane_id": "w2:p1"},
            }}))
        if command == "worktree remove":
            # Herdr refuses a dirty checkout without --force, and closes the workspace.
            path = self.last_worktree
            if sh(path, "git", "status", "--porcelain") and "--force" not in argv:
                return Result(1, json.dumps({"error": {
                    "code": "dirty_worktree_requires_force", "message": "contains modified files"}}))
            repo = Path(sh(path, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
            sh(repo, "git", "worktree", "remove", *(["--force"] if "--force" in argv else []), str(path))
            return Result(0, json.dumps({"result": {"type": "worktree_removed", "path": str(path)}}))
        if command in ("workspace report-metadata",):
            return Result(0, "")
        if command == "worktree open":
            opts = dict(zip(argv[3::2], argv[4::2]))
            return Result(0, json.dumps({"result": {
                "worktree": {"path": opts["--path"], "branch": "placido/01-shout-flag"},
                "workspace": {"workspace_id": "w3"}, "root_pane": {"pane_id": "w3:p1"},
            }}))
        if command == "workspace list":
            return Result(0, json.dumps({"result": {"type": "workspace_list", "workspaces": self.workspaces}}))
        return Result(0, json.dumps({"result": {"type": "ok"}}))

    def tokens(self) -> list[str]:
        return [f"placido={value}" for value in self.statuses]


class StartTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.runs = self.tmp / "runs"
        (self.tmp / "worktrees").mkdir()
        self.fake = FakeHerdr(self.tmp / "worktrees")
        patch = mock.patch.object(doctor, "tool_versions", return_value={"herdr": "0.9.3"})
        patch.start()
        self.addCleanup(patch.stop)
        shown = mock.patch.object(status, "show", side_effect=lambda run_dir, value: self.fake.statuses.append(value))
        shown.start()
        self.addCleanup(shown.stop)

    def start(self, arg: str, repo: Path, cwd: Path | None = None, gh=None) -> runlog.Run:
        return start.start(
            arg, cwd or repo, herdr=Herdr(self.fake), runs_root=self.runs, gh=gh or self.no_gh
        )

    @staticmethod
    def no_gh(argv: list[str]) -> Result:
        raise AssertionError(f"unexpected gh call: {argv}")

    def events(self, run_path: Path) -> list[str]:
        return [event["event"] for event in runlog.read_events(run_path)]


class StartTest(StartTestCase):
    def test_creates_the_worktree_runs_setup_and_logs_it_all(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        self.assertEqual(run.path.parent, self.runs / "proj" / "01-shout-flag")
        self.assertEqual(
            self.events(run.path),
            ["run.start", "worktree.created", "setup.start", "setup.end", "run.ready", "workspace.focused"],
        )
        worktree = self.tmp / "worktrees" / "placido-01-shout-flag"
        self.assertTrue((worktree / "setup-ran").exists(), "setup runs inside the worktree")
        self.assertFalse((repo / "setup-ran").exists())
        self.assertEqual(sh(worktree, "git", "branch", "--show-current"), "placido/01-shout-flag")
        self.assertEqual(self.fake.tokens(), ["placido=working · setting up", "placido=ready · run placido spec"])

    def test_run_json_records_where_the_run_starts(self):
        repo = make_repo(self.tmp)
        run = self.start("01-shout-flag", repo)
        record = json.loads((run.path / "run.json").read_text())
        self.assertEqual(record["base"], "main")
        self.assertEqual(record["base_commit"], sh(repo, "git", "rev-parse", "HEAD"))
        self.assertEqual(record["branch"], "placido/01-shout-flag")
        self.assertEqual(record["repo"], str(repo))
        self.assertEqual(record["config"], str(repo / ".placido" / "config.toml"))
        self.assertEqual(record["versions"], {"herdr": "0.9.3"})

    def test_worktree_event_names_the_workspace(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        created = next(e for e in runlog.read_events(run.path) if e["event"] == "worktree.created")
        self.assertEqual(created["workspace"], "w2")
        self.assertEqual(created["pane"], "w2:p1")
        self.assertEqual(created["branch"], "placido/01-shout-flag")

    def test_the_issue_text_is_saved_in_the_run(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        self.assertEqual((run.path / "issue.md").read_text(), "# Shout\n")
        record = json.loads((run.path / "run.json").read_text())
        self.assertEqual(record["issue_title"], "Shout")
        self.assertEqual(record["issue_source"], str(repo / "issues" / "01-shout-flag.md"))

    def test_github_issue_from_the_origin_remote(self):
        repo = make_repo(self.tmp)
        sh(repo, "git", "remote", "add", "origin", "git@github.com:leodip/goiabada.git")
        calls = []

        def gh(argv: list[str]) -> Result:
            calls.append(argv)
            return Result(0, json.dumps({
                "number": 439, "title": "Rate limiter split", "body": "Do it.", "labels": [],
                "state": "OPEN", "url": "https://github.com/leodip/goiabada/issues/439", "comments": [],
            }))

        run = self.start("439", repo, gh=gh)
        self.assertEqual(run.path.parent.name, "439-rate-limiter-split")
        self.assertEqual(calls[0][3:6], ["439", "--repo", "leodip/goiabada"])
        self.assertIn("Do it.", (run.path / "issue.md").read_text())
        worktree = self.tmp / "worktrees" / "placido-439-rate-limiter-split"
        self.assertEqual(sh(worktree, "git", "branch", "--show-current"), "placido/439-rate-limiter-split")
        first = next(runlog.read_events(run.path))
        self.assertEqual(first["source"], "https://github.com/leodip/goiabada/issues/439")

    def test_project_root_from_inside_a_worktree(self):
        repo = make_repo(self.tmp)
        self.start("01", repo)
        worktree = self.tmp / "worktrees" / "placido-01-shout-flag"
        self.assertEqual(start.project_root(worktree), repo)
        self.assertEqual(start.project_root(repo / "issues"), repo)

    def test_works_from_a_subfolder(self):
        repo = make_repo(self.tmp)
        run = self.start("02", repo, cwd=repo / "issues")
        self.assertEqual(run.path.parent.name, "02-many-names")

    def test_herdr_gets_the_branch_base_and_label(self):
        repo = make_repo(self.tmp)
        self.start("01", repo)
        create = next(argv for argv in self.fake.calls if argv[1:3] == ["worktree", "create"])
        self.assertEqual(
            create[3:],
            ["--cwd", str(repo), "--branch", "placido/01-shout-flag", "--base", "main",
             "--label", "01-shout-flag", "--no-focus"],
        )

    def test_no_setup_command_skips_setup(self):
        repo = make_repo(self.tmp, setup="")
        run = self.start("01", repo)
        self.assertEqual(self.events(run.path), ["run.start", "worktree.created", "run.ready", "workspace.focused"])
        self.assertEqual(self.fake.tokens(), ["placido=ready · run placido spec"])


class FocusTest(StartTestCase):
    """Once setup is done, Herdr switches the user to the issue's workspace, and
    placido says where the worktree is and what comes next."""

    def test_the_new_workspace_is_focused_after_setup(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        calls = [argv[1:4] for argv in self.fake.calls]
        self.assertEqual(calls[-1], ["workspace", "focus", "w2"])
        self.assertLess(self.events(run.path).index("run.ready"), self.events(run.path).index("workspace.focused"))
        text = start.next_steps(run)
        self.assertIn("Herdr has switched to the issue's workspace", text)
        self.assertIn(f"  cd {self.tmp / 'worktrees' / 'placido-01-shout-flag'}\n  placido spec\n", text)

    def test_a_failed_setup_does_not_move_the_user(self):
        repo = make_repo(self.tmp, setup="exit 3")
        with self.assertRaises(start.StartError):
            self.start("01", repo)
        self.assertNotIn(["workspace", "focus"], [argv[1:3] for argv in self.fake.calls])

    def test_focus_failing_is_only_a_warning(self):
        repo = make_repo(self.tmp)
        self.fake.fail["workspace focus"] = Result(1, '{"error":{"code":"workspace_not_found","message":"gone"}}')
        run = self.start("01", repo)
        self.assertIn("herdr.warning", self.events(run.path))
        self.assertIn("The issue's workspace in Herdr has a pane in the worktree.", start.next_steps(run))

    def test_a_sealed_agreement_means_run_next(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        with mock.patch.object(start.spec, "sealed", return_value=True):
            self.assertTrue(start.next_steps(run).endswith("placido run\n"))


class RepoLabelTest(StartTestCase):
    """The repository's workspace groups the issues in the sidebar; placido keeps it
    named after the repository, whatever folder its pane is in."""

    def repo_workspace(self, repo: Path, label: str) -> None:
        self.fake.workspaces = [
            {"workspace_id": "w7", "label": label,
             "worktree": {"checkout_path": str(repo), "is_linked_worktree": False}},
            {"workspace_id": "w9", "label": "placido-01-shout-flag",
             "worktree": {"checkout_path": str(repo / "elsewhere"), "is_linked_worktree": True}},
        ]

    def test_an_issue_label_on_the_repository_workspace_is_replaced(self):
        repo = make_repo(self.tmp)
        self.repo_workspace(repo, "placido-440-admin-console")
        run = self.start("01", repo)
        self.assertIn(["herdr", "workspace", "rename", "w7", "proj"], self.fake.calls)
        renamed = runlog.last_event(run.path, "workspace.renamed")
        self.assertEqual((renamed["label"], renamed["was"]), ("proj", "placido-440-admin-console"))

    def test_a_right_label_is_left_alone(self):
        repo = make_repo(self.tmp)
        self.repo_workspace(repo, "proj")
        self.start("01", repo)
        self.assertFalse(any(argv[1:3] == ["workspace", "rename"] for argv in self.fake.calls))

    def test_no_repository_workspace_is_fine(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        self.assertNotIn("workspace.renamed", self.events(run.path))
        self.assertIn("run.ready", self.events(run.path))


class ReopenTest(StartTestCase):
    """An issue's workspace closed from Herdr's sidebar is reopened on its worktree, so
    the next step has somewhere to open its tab (#404)."""

    def test_a_closed_workspace_is_reopened_and_recorded(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        self.fake.fail["workspace get"] = Result(
            1, '{"error":{"code":"workspace_not_found","message":"workspace w2 not found"}}')
        created = start.ensure_workspace(run.path, Herdr(self.fake))
        self.assertEqual((created["workspace"], created["pane"], created["was"]), ("w3", "w3:p1", "w2"))
        self.assertTrue(created["reopened"])
        opened = next(argv for argv in self.fake.calls if argv[1:3] == ["worktree", "open"])
        self.assertEqual(opened[opened.index("--cwd") + 1], str(repo))
        self.assertEqual(opened[opened.index("--label") + 1], "01-shout-flag")
        self.assertEqual(start.select_run(repo, self.runs), run.path)  # still the active run

    def test_an_open_workspace_is_left_alone(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        self.assertEqual(start.ensure_workspace(run.path, Herdr(self.fake))["workspace"], "w2")
        self.assertFalse(any(argv[1:3] == ["worktree", "open"] for argv in self.fake.calls))

    def test_other_herdr_errors_are_not_hidden(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        self.fake.fail["workspace get"] = Result(1, '{"error":{"code":"server_down","message":"no server"}}')
        with self.assertRaises(HerdrError):
            start.ensure_workspace(run.path, Herdr(self.fake))


class EnvTest(StartTestCase):
    def test_setup_sees_the_run_variables(self):
        repo = make_repo(self.tmp, setup="env | grep ^PLACIDO_ | sort > vars.txt")
        (repo / ".gitignore").write_text("setup-ran\nvars.txt\n")
        sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "ignore")
        run = self.start("01", repo)
        worktree = self.tmp / "worktrees" / "placido-01-shout-flag"
        self.assertEqual(
            (worktree / "vars.txt").read_text().splitlines(),
            [
                "PLACIDO_ISSUE=01-shout-flag",
                "PLACIDO_ISSUE_NUMBER=",
                "PLACIDO_NAME=proj-01-shout-flag",
                f"PLACIDO_RUN_DIR={run.path}",
                f"PLACIDO_WORKTREE={worktree}",
            ],
        )
        self.assertEqual(start.env_of(run.path)["PLACIDO_WORKTREE"], str(worktree))

    def test_github_issue_name_uses_the_number(self):
        env = start.run_env("Goiabada", "439-rate-limiter", 439, Path("/w"), Path("/r"))
        self.assertEqual(env["PLACIDO_NAME"], "goiabada-439")
        self.assertEqual(env["PLACIDO_ISSUE_NUMBER"], "439")


class SetupFailureTest(StartTestCase):
    def test_failed_setup_ends_the_run_and_notifies(self):
        repo = make_repo(self.tmp, setup="echo broken; exit 3")
        with self.assertRaisesRegex(start.StartError, "setup failed with exit 3"):
            self.start("01", repo)
        (run,) = (self.runs / "proj" / "01-shout-flag").iterdir()
        self.assertEqual(
            self.events(run), ["run.start", "worktree.created", "setup.start", "setup.end", "run.end"]
        )
        self.assertEqual(runlog.outcome(run), "failed")
        self.assertEqual((run / "setup.log").read_text(), "broken\n")
        self.assertEqual(self.fake.tokens()[-1], "placido=stopped · setup failed")
        self.assertTrue(any(argv[1:3] == ["notification", "show"] for argv in self.fake.calls))


class SetupRetryTest(StartTestCase):
    """A failed setup is fixed on the base, then `placido start` again retries it in
    the same worktree (first seen on Goiabada: a setup script committed without its
    executable bit)."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = make_repo(self.tmp, setup="test -f fixed")
        self.worktree = self.tmp / "worktrees" / "placido-01-shout-flag"
        with self.assertRaisesRegex(start.StartError, "run `placido start 01` again"):
            self.start("01", self.repo)
        (self.failed,) = (self.runs / "proj" / "01-shout-flag").iterdir()
        os.utime(self.failed / "run.json", (1, 1))  # started before the retry

    def fix_on_main(self) -> str:
        (self.repo / "fixed").write_text("")
        sh(self.repo, "git", "add", "fixed")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fix setup")
        return sh(self.repo, "git", "rev-parse", "HEAD")

    def test_retries_setup_in_the_same_worktree_brought_up_to_the_base(self):
        fixed = self.fix_on_main()
        creates = sum(argv[1:3] == ["worktree", "create"] for argv in self.fake.calls)
        run = self.start("01", self.repo)
        self.assertEqual(sum(argv[1:3] == ["worktree", "create"] for argv in self.fake.calls), creates)
        self.assertEqual(
            self.events(run.path),
            ["run.start", "worktree.created", "setup.start", "setup.end", "run.ready", "workspace.focused"],
        )
        self.assertEqual(runlog.last_event(run.path, "run.start")["retry_of"], f"01-shout-flag/{self.failed.name}")
        created = runlog.last_event(run.path, "worktree.created")
        self.assertTrue(created["reused"])
        self.assertEqual((created["path"], created["workspace"]), (str(self.worktree), "w2"))
        self.assertEqual(sh(self.worktree, "git", "rev-parse", "HEAD"), fixed)
        self.assertEqual(runlog.read_record(run.path)["base_commit"], fixed)
        self.assertEqual(start.select_run(self.repo, self.runs), run.path)
        self.assertEqual(start.select_run(self.worktree, self.runs), run.path)

    def test_a_fix_made_in_the_worktree_itself_also_works(self):
        (self.worktree / "fixed").write_text("")
        run = self.start("01", self.repo)
        self.assertIn("run.ready", self.events(run.path))

    def test_failing_again_can_be_retried_again(self):
        with self.assertRaisesRegex(start.StartError, "setup failed"):
            self.start("01", self.repo)
        for folder in (self.runs / "proj" / "01-shout-flag").iterdir():
            os.utime(folder / "run.json", (1, 1))
        self.fix_on_main()
        run = self.start("01", self.repo)
        self.assertIn("run.ready", self.events(run.path))

    def test_commits_on_the_branch_mean_something_else_happened(self):
        (self.worktree / "work.txt").write_text("")
        sh(self.worktree, "git", "add", "work.txt")
        sh(self.worktree, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "work")
        with self.assertRaisesRegex(start.StartError, "already exists: `placido run` resumes"):
            self.start("01", self.repo)

    def test_a_closed_run_is_not_retried(self):
        runlog.Run(self.failed).event("run.closed")
        with self.assertRaisesRegex(start.StartError, "already exists"):
            self.start("01", self.repo)

    def test_a_branch_whose_setup_passed_is_not_retried(self):
        repo = make_repo(self.tmp / "other")
        self.start("02", repo)
        with self.assertRaisesRegex(start.StartError, "`placido close 02` ends one"):
            self.start("02", repo)

    def test_a_failed_setup_is_closable_but_not_active(self):
        self.assertEqual(runlog.active_runs(self.runs, "proj", str(self.repo)), [])
        self.assertEqual(start.find_run(self.repo, self.runs, "01", unready=True), self.failed)
        self.assertEqual(start.select_run(self.repo, self.runs, unready=True), self.failed)


class RefusalTest(StartTestCase):
    def test_not_a_git_repository(self):
        with self.assertRaisesRegex(start.StartError, "not inside a git repository"):
            self.start("01", self.tmp)

    def test_unknown_issue(self):
        repo = make_repo(self.tmp)
        with self.assertRaisesRegex(start.StartError, "no issue '9'"):
            self.start("9", repo)

    def test_ambiguous_issue(self):
        repo = make_repo(self.tmp)
        with self.assertRaisesRegex(start.StartError, "ambiguous: 01-shout-flag, 02-many-names"):
            self.start("0", repo)

    def test_existing_branch_is_refused_before_any_run_folder(self):
        repo = make_repo(self.tmp)
        sh(repo, "git", "branch", "placido/01-shout-flag")
        with self.assertRaisesRegex(start.StartError, "already exists"):
            self.start("01", repo)
        self.assertFalse(self.runs.exists())

    def test_missing_base(self):
        repo = make_repo(self.tmp, base="develop")
        with self.assertRaisesRegex(start.StartError, "base 'develop' is not a commit"):
            self.start("01", repo)

    def test_herdr_failure_ends_the_run(self):
        repo = make_repo(self.tmp)
        self.fake.fail["worktree create"] = Result(
            1, '{"error":{"code":"worktree_create_failed","message":"fatal: already exists"}}'
        )
        with self.assertRaisesRegex(start.StartError, "fatal: already exists"):
            self.start("01", repo)
        (run,) = (self.runs / "proj" / "01-shout-flag").iterdir()
        self.assertEqual(self.events(run), ["run.start", "worktree.failed", "run.end"])


class SelectRunTest(StartTestCase):
    """Several issues in flight at once: each command must act on the right one."""

    def test_one_run_is_found_from_anywhere(self):
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        worktree = self.tmp / "worktrees" / "placido-01-shout-flag"
        self.assertEqual(start.select_run(repo, self.runs), run.path)
        self.assertEqual(start.select_run(worktree, self.runs), run.path)

    def test_each_worktree_selects_its_own_run(self):
        repo = make_repo(self.tmp)
        first = self.start("01", repo)
        second = self.start("02", repo)
        trees = self.tmp / "worktrees"
        self.assertEqual(start.select_run(trees / "placido-01-shout-flag", self.runs), first.path)
        self.assertEqual(start.select_run(trees / "placido-02-many-names", self.runs), second.path)

    def test_the_main_checkout_refuses_to_guess(self):
        repo = make_repo(self.tmp)
        self.start("01", repo)
        self.start("02", repo)
        with self.assertRaisesRegex(start.StartError, "2 issues are in progress") as caught:
            start.select_run(repo, self.runs)
        self.assertIn("01-shout-flag: cd ", str(caught.exception))
        self.assertIn("02-many-names: cd ", str(caught.exception))

    def test_closed_runs_do_not_count(self):
        repo = make_repo(self.tmp)
        first = self.start("01", repo)
        second = self.start("02", repo)
        first.event("run.closed")
        self.assertEqual(start.select_run(repo, self.runs), second.path)

    def test_an_ended_run_stays_selectable_until_closed(self):
        # Its branch may still be tried, reviewed again, or cleaned up with placido close.
        repo = make_repo(self.tmp)
        run = self.start("01", repo)
        run.event("run.end", outcome="passed")
        self.assertEqual(start.select_run(repo, self.runs), run.path)

    def test_a_run_whose_worktree_is_gone_does_not_count(self):
        repo = make_repo(self.tmp)
        self.start("01", repo)
        second = self.start("02", repo)
        sh(repo, "git", "worktree", "remove", "--force",
           str(self.tmp / "worktrees" / "placido-01-shout-flag"))
        self.assertEqual(start.select_run(repo, self.runs), second.path)

    def test_a_newer_run_takes_over_the_same_worktree(self):
        # The repository was rebuilt and the issue started again at the same path:
        # only the newer run can still act there.
        repo = make_repo(self.tmp)
        old = self.start("01", repo)
        worktree = self.tmp / "worktrees" / "placido-01-shout-flag"
        sh(repo, "git", "worktree", "remove", "--force", str(worktree))
        sh(repo, "git", "branch", "-D", "placido/01-shout-flag")
        os.utime(old.path / "run.json", (1, 1))  # started long ago
        new = self.start("01", repo)
        self.assertEqual(start.select_run(repo, self.runs), new.path)
        self.assertEqual(start.select_run(worktree, self.runs), new.path)

    def test_a_worktree_without_a_run(self):
        repo = make_repo(self.tmp)
        sh(repo, "git", "worktree", "add", "-q", "-b", "other", str(self.tmp / "other"))
        with self.assertRaisesRegex(start.StartError, "no active placido run uses this worktree"):
            start.select_run(self.tmp / "other", self.runs)

    def test_projects_sharing_a_folder_name_do_not_mix(self):
        repo = make_repo(self.tmp)
        self.start("01", repo)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        other = make_repo(elsewhere)  # also called "proj", in another place
        with self.assertRaisesRegex(start.StartError, "no active run for proj"):
            start.select_run(other, self.runs)


class RealHerdrGuardTest(unittest.TestCase):
    def test_the_suite_never_reaches_the_real_herdr(self):
        # tests/__init__.py makes every Herdr built without a runner refuse to run.
        with self.assertRaisesRegex(AssertionError, "a test reached the real herdr: herdr workspace list"):
            Herdr().call("workspace", "list")


class HerdrCallTest(unittest.TestCase):
    def herdr(self, result: Result) -> Herdr:
        return Herdr(lambda argv, timeout=None: result)

    def test_returns_the_result(self):
        self.assertEqual(self.herdr(Result(0, '{"result":{"a":1}}')).call("x", "y"), {"a": 1})

    def test_silent_success(self):
        self.assertEqual(self.herdr(Result(0, "")).call("x", "y"), {})

    def test_json_error(self):
        with self.assertRaisesRegex(HerdrError, "herdr x y: boom"):
            self.herdr(Result(1, '{"error":{"message":"boom"}}')).call("x", "y")

    def test_unreadable_failure(self):
        with self.assertRaisesRegex(HerdrError, "herdr x y: herdr: not found"):
            self.herdr(Result(127, "herdr: not found")).call("x", "y")

    def test_warning_lines_before_the_json_are_skipped(self):
        herdr = self.herdr(Result(0, 'warning: something\n{"result":{"ok":true}}'))
        self.assertEqual(herdr.call("x", "y"), {"ok": True})

    def test_create_worktree_reads_the_ids(self):
        out = json.dumps({"result": {
            "worktree": {"path": "/w/p", "branch": "placido/x"},
            "workspace": {"workspace_id": "w9"},
            "root_pane": {"pane_id": "w9:p1"},
        }})
        tree = self.herdr(Result(0, out)).create_worktree(Path("/r"), "placido/x", "main", "x")
        self.assertEqual(tree, Worktree(Path("/w/p"), "placido/x", "w9", "w9:p1"))


class StartCommandTest(StartTestCase):
    def test_cli_reports_errors_without_a_traceback(self):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err), redirect_stdout(out), mock.patch.object(
            Path, "cwd", return_value=self.tmp
        ):
            code = cli.main(["start", "01"])
        self.assertEqual(code, 1)
        self.assertIn("placido: ", err.getvalue())
        self.assertIn("not inside a git repository", err.getvalue())


if __name__ == "__main__":
    unittest.main()
