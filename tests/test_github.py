import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from placido import config, driver, github, runlog
from placido.proc import Result
from tests.test_checks import FakeFixer

PR = "https://github.com/leodip/goiabada/pull/450"


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGh:
    """Answers git push and gh like GitHub would; checks follow a script, one per poll."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.checks: list[list[dict]] = []  # successive answers of `gh pr checks`
        self.issues = 460
        self.bodies: dict[str, str] = {}

    def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        if argv[0] == "git":
            return Result(0, "pushed")
        what = " ".join(argv[1:3])
        if what in ("pr create", "pr edit", "issue create"):
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            self.bodies.setdefault(what, body)
            self.bodies[what + " last"] = body
        if what == "pr create":
            return Result(0, f"Creating pull request\n{PR}\n")
        if what == "issue create":
            self.issues += 1
            return Result(0, f"https://github.com/leodip/goiabada/issues/{self.issues}\n")
        if what == "pr checks":
            answer = self.checks.pop(0) if self.checks else []
            return Result(0, json.dumps(answer)) if answer else Result(1, "no checks reported on the 'x' branch")
        if what == "run view":
            return Result(0, "build\tFAIL\tgreet_test.go:12: want Hello\n")
        return Result(0, "")

    def made(self, what: str) -> int:
        return sum(1 for argv in self.calls if " ".join(argv[1:3]) == what)


def check(name: str, bucket: str) -> dict:
    return {"name": name, "bucket": bucket, "workflow": "Check",
            "link": "https://github.com/leodip/goiabada/actions/runs/111/job/222"}


class DeliverTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "goiabada"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "user.email", "t@t")
        sh(self.repo, "git", "remote", "add", "origin", "https://github.com/leodip/goiabada.git")
        (self.repo / "README").write_text("hi\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        self.run = runlog.Run.create(self.tmp / "runs", "goiabada", "439-rate-limit", {
            "repo": str(self.repo), "branch": "placido/439-rate-limit", "base": "main",
            "issue_title": "Rate limiter counts half moves", "issue_number": 439,
        })
        (self.run.path.parent / "agreement.md").write_text(
            "# Agreement\n\n## Decisions\n1. x\n\n### Folded in\n- #440 shares the counter\n- #439 itself\n\n## Seams\n- #999 not this\n"
        )
        self.run.event("review.followups", items=[
            {"id": "F1", "title": "Expose the limit in metrics", "description": "no gauge yet", "why": "separate feature"},
        ])
        self.gh = FakeGh()
        self.hub = github.GitHub("leodip/goiabada", self.repo, self.gh)
        self.fixer = FakeFixer(self.run, self.repo)
        self.now = 0.0

    def ctx(self) -> driver.Context:
        cfg = self.tmp / "config.toml"
        cfg.write_text('[commands.full]\nrun = "test -f ok.txt"\n[gates]\nfinal = ["full"]\n')
        return driver.Context(self.run, self.fixer, None, config.load(cfg),
                              config.AgentSpec("claude", "opus", "high"), self.repo, {})

    def deliver(self) -> str:
        def sleep(seconds: float) -> None:
            self.now += seconds
        return github.deliver(self.ctx(), config.AgentSpec("claude", "opus", "high"), self.hub,
                              sleep=sleep, clock=lambda: self.now)

    def events(self, kind: str) -> list[dict]:
        return [e for e in runlog.read_events(self.run.path) if e["event"] == kind]

    def test_without_a_github_remote_the_branch_stays_local(self):
        sh(self.repo, "git", "remote", "remove", "origin")
        self.assertEqual(github.deliver(self.ctx(), None, None), "local")
        self.assertEqual(len(self.events("github.skipped")), 1)

    def test_push_open_file_follow_ups_and_wait_for_green(self):
        self.gh.checks = [[], [check("build", "pending")], [check("build", "pass"), check("lint", "skipping")]]
        self.assertEqual(self.deliver(), "green")
        self.assertEqual(self.gh.calls[0][:5], ["git", "-C", str(self.repo), "push", "--set-upstream"])
        create = next(argv for argv in self.gh.calls if argv[1:3] == ["pr", "create"])
        self.assertEqual(create[create.index("--title") + 1], "Rate limiter counts half moves")
        self.assertEqual(create[create.index("--base") + 1], "main")
        body = self.gh.bodies["pr create"]
        self.assertTrue(body.startswith("Closes #439\nCloses #440\n\n- **Branch:**"), body[:80])
        self.assertNotIn("**State:**", body)
        self.assertNotIn("#999", body.split("\n\n")[0])
        self.assertIn("Built by placido", body)
        issue = self.gh.bodies["issue create"]
        self.assertIn(f"**Left out of {PR} because:** separate feature", issue)
        self.assertIn("Found by placido while working on #439.", issue)
        final = self.gh.bodies["pr edit last"]
        self.assertIn("## Follow-up issues\n\n- https://github.com/leodip/goiabada/issues/461 Expose the limit", final)
        self.assertIn(f"- **Pull request:** {PR}", final)
        self.assertIn("- **CI:** green", final)
        self.assertEqual(self.events("pr.opened")[0]["number"], 450)
        self.assertEqual(self.events("ci.result")[-1]["outcome"], "green")

    def test_a_red_run_is_fixed_pushed_and_checked_again(self):
        self.gh.checks = [[check("build", "fail")], [check("build", "pass")]]
        self.assertEqual(self.deliver(), "green")
        log = (self.run.path / "ci" / "1" / "01-Check-build.log").read_text()
        self.assertIn("greet_test.go:12: want Hello", log)
        self.assertIn("ci/1/01-Check-build.log", self.fixer.prompts[0])
        self.assertIn("Placido-Check-Fix: ci-1", sh(self.repo, "git", "log", "-1", "--format=%B"))
        self.assertEqual(len(self.events("github.pushed")), 2)
        self.assertEqual([e["outcome"] for e in self.events("ci.result")], ["red", "green"])

    def test_red_after_the_fixes(self):
        self.gh.checks = [[check("build", "fail")]] * 3
        self.assertEqual(self.deliver(), "red")
        self.assertEqual(len(self.fixer.prompts), 2)
        self.assertIn("- **CI:** red after placido's fixes (build)", self.gh.bodies["pr edit last"])

    def test_no_ci_at_all(self):
        self.assertEqual(self.deliver(), "none")
        self.assertGreaterEqual(self.now, github.ATTACH_SECONDS)

    def test_ci_still_running_when_the_wait_ends(self):
        self.gh.checks = [[check("build", "pending")]] * 500
        self.assertEqual(self.deliver(), "pending")

    def test_a_resumed_delivery_reuses_the_pull_request_and_issues(self):
        self.gh.checks = [[check("build", "pass")]] * 2
        self.deliver()
        self.deliver()
        self.assertEqual((self.gh.made("pr create"), self.gh.made("issue create")), (1, 1))
        self.assertEqual(self.gh.made("pr edit"), 4)


class FoldedTest(unittest.TestCase):
    def test_only_the_folded_in_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(github.folded_issues(folder), [])
            (folder / "agreement.md").write_text("## Decisions\n- #1\n## Folded in\n- #5 and #7, #5 again\n## Seams\n#9\n")
            self.assertEqual(github.folded_issues(folder), [5, 7])
