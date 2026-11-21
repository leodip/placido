import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from placido import config, driver, github, runlog, spec
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
        self.repo: Path | None = None  # the pull request's head is this checkout's HEAD
        self.lag = 0  # after each push, how many polls GitHub still has the previous head
        self.behind = 0

    def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        if argv[0] == "git":
            self.behind = self.lag
            return Result(0, "pushed")
        what = " ".join(argv[1:3])
        if what in ("pr create", "pr edit", "issue create", "issue comment", "pr comment"):
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            self.bodies.setdefault(what, body)
            self.bodies[what + " last"] = body
        if what == "pr create":
            return Result(0, f"Creating pull request\n{PR}\n")
        if what == "issue create":
            self.issues += 1
            return Result(0, f"https://github.com/leodip/goiabada/issues/{self.issues}\n")
        if what == "issue comment":
            return Result(0, f"https://github.com/leodip/goiabada/issues/{argv[3]}#issuecomment-9001\n")
        if what == "pr comment":
            return Result(0, f"https://github.com/leodip/goiabada/pull/{argv[3]}#issuecomment-9002\n")
        if what == "pr view":
            if self.behind:
                self.behind -= 1
                return Result(0, "0" * 40 + "\n")
            return Result(0, sh(self.repo, "git", "rev-parse", "HEAD") + "\n")
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


class DeliverCase(unittest.TestCase):
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
            "issue_source": "https://github.com/leodip/goiabada/issues/439",
        })
        (self.run.path.parent / "agreement.md").write_text(
            "# Agreement\n\n## Decisions\n1. x\n\n### Folded in\n- #440 shares the counter\n- #439 itself\n\n## Seams\n- #999 not this\n"
        )
        self.run.event("review.followups", items=[
            {"id": "F1", "title": "Expose the limit in metrics", "description": "no gauge yet", "why": "separate feature"},
        ])
        self.gh = FakeGh()
        self.gh.repo = self.repo
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


class DeferralTest(DeliverCase):
    DEFERRAL = spec.Deferral("start once tool 2.16.0 is out", "The pinned 2.15.0 bundles the old analyzer.")

    def test_the_trigger_and_the_reasons_go_on_the_issue(self):
        url = github.post_deferral(self.run, self.repo, self.DEFERRAL, self.hub)
        self.assertEqual(url, "https://github.com/leodip/goiabada/issues/439#issuecomment-9001")
        comment = next(argv for argv in self.gh.calls if argv[1:3] == ["issue", "comment"])
        self.assertEqual(comment[3:6], ["439", "-R", "leodip/goiabada"])
        self.assertEqual(self.gh.bodies["issue comment"],
                         "**Deferred:** start once tool 2.16.0 is out\n\nThe pinned 2.15.0 bundles the old analyzer.\n\n"
                         f"From placido's run `439-rate-limit/{self.run.path.name}`, which ends here with nothing built.\n")
        self.assertEqual([(e["issue"], e["url"]) for e in self.events("deferral.posted")], [(439, url)])

    def test_an_issue_not_on_github_gets_nothing(self):
        record = runlog.read_record(self.run.path)
        del record["issue_number"]
        (self.run.path / "run.json").write_text(json.dumps({**record, "issue_source": "/repo/issues/02-x.md"}))
        self.assertIsNone(github.post_deferral(self.run, self.repo, self.DEFERRAL, self.hub))
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.events("deferral.posted"), [])

    def test_it_goes_to_the_issue_s_own_repository_whatever_the_origin(self):
        # Codex, reviewing 444aead: the checkout's origin decided where it went.
        sh(self.repo, "git", "remote", "remove", "origin")
        for origin in (None, "https://github.com/someone/fork.git"):
            if origin:
                sh(self.repo, "git", "remote", "add", "origin", origin)
            with self.subTest(origin=origin):
                github.post_deferral(self.run, self.repo, self.DEFERRAL, self.hub)
                comment = [argv for argv in self.gh.calls if argv[1:3] == ["issue", "comment"]][-1]
                self.assertEqual(comment[3:6], ["439", "-R", "leodip/goiabada"])


class DeliverTest(DeliverCase):
    def test_without_a_github_remote_the_branch_stays_local(self):
        sh(self.repo, "git", "remote", "remove", "origin")
        self.assertEqual(github.deliver(self.ctx(), None, None), "local")
        self.assertEqual(len(self.events("github.skipped")), 1)

    def test_push_open_post_follow_ups_and_wait_for_green(self):
        self.gh.checks = [[], [check("build", "pending")], [check("build", "pass"), check("lint", "skipping")]]
        self.assertEqual(self.deliver(), "green")
        self.assertEqual(self.gh.calls[0][:5], ["git", "-C", str(self.repo), "push", "--set-upstream"])
        create = next(argv for argv in self.gh.calls if argv[1:3] == ["pr", "create"])
        self.assertEqual(create[create.index("--title") + 1], "Rate limiter counts half moves")
        self.assertEqual(create[create.index("--base") + 1], "main")
        body = self.gh.bodies["pr create"]
        self.assertTrue(body.startswith("Closes #439\nCloses #440\n\n"), body[:80])
        for run_detail in ("**State:**", "**Branch:**", "**Time:**", "Along the way"):
            self.assertNotIn(run_detail, body)
        self.assertNotIn("#999", body.split("\n\n")[0])
        self.assertIn("Built by placido", body)
        self.assertEqual(self.gh.made("issue create"), 0)  # placido files no issues
        comment = self.gh.bodies["pr comment"]
        self.assertIn("### 1. Expose the limit in metrics\n\nLeft out because: separate feature", comment)
        self.assertIn("gh issue create --title 'Expose the limit in metrics' --body-file ", comment)
        final = self.gh.bodies["pr edit last"]
        self.assertIn("[the follow-ups comment](https://github.com/leodip/goiabada/pull/450#issuecomment-9002)", final)
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

    def test_checks_count_only_once_the_pull_request_is_on_the_pushed_commit(self):
        # Meanwhile gh pr checks answers the previous commit's: after a fix, the old red.
        self.gh.lag = 2
        self.gh.checks = [[check("build", "fail")], [check("build", "pass")]]
        self.assertEqual(self.deliver(), "green")
        self.assertEqual([e["outcome"] for e in self.events("ci.result")], ["red", "green"])
        self.assertEqual(self.gh.made("pr view"), 6)  # three polls after each push
        self.assertEqual(self.gh.made("pr checks"), 2)

    def test_a_pull_request_stuck_on_the_previous_commit_is_still_pending(self):
        self.gh.lag = 10**6
        self.assertEqual(self.deliver(), "pending")
        self.assertEqual(self.gh.made("pr checks"), 0)

    def test_red_after_the_fixes_and_a_run_again(self):
        # The second fix fails its checks, so nothing is pushed: the failed jobs run again.
        self.gh.checks = [[check("build", "fail")]] * 2 + [[check("build", "pending")], [check("build", "fail")]]
        self.assertEqual(self.deliver(), "red")
        self.assertEqual(len(self.fixer.prompts), 2)
        self.assertEqual(self.gh.made("run rerun"), 1)
        self.assertEqual(len(self.events("checks.fix_failed")), 1)
        self.assertEqual([e["outcome"] for e in self.events("ci.result")], ["red", "red", "red"])
        self.assertIn("> **CI is red after placido's fixes (build).**", self.gh.bodies["pr edit last"])

    def test_a_failure_the_fixer_finds_outside_the_change_runs_again_once(self):
        # #869: a test whose random address spelled "href" failed once; the fixer changed
        # nothing, and the run stopped red with no action taken (2026-11-20).
        self.fixer.unchanged = "No code changes\n\nA flaky test.\n\n## Evidence\n- x\n"
        rerun = dict(check("build", "fail"), link="https://github.com/leodip/goiabada/actions/runs/111/job/333")
        self.gh.checks = [
            [check("build", "fail"), check("CodeRabbit", "pass")],
            [check("build", "fail")],  # gh still answers the old failure: not the rerun's outcome
            [dict(rerun, bucket="pending")],
            [dict(rerun, bucket="pass")],
        ]
        self.assertEqual(self.deliver(), "green")
        rerun_calls = [argv for argv in self.gh.calls if argv[1:3] == ["run", "rerun"]]
        self.assertEqual(rerun_calls, [["gh", "run", "rerun", "111", "--failed", "-R", "leodip/goiabada"]])
        self.assertEqual(self.events("ci.rerun")[0]["runs"], ["111"])
        self.assertEqual([e["outcome"] for e in self.events("ci.result")], ["red", "green"])
        self.assertEqual((len(self.fixer.prompts), len(self.events("github.pushed"))), (1, 1))

    def test_a_rerun_check_counts_only_once_it_is_listed_again(self):
        # #869: 1.6 seconds after the rerun, gh listed every check but the rerun one, and
        # placido read the others' green as the outcome (2026-11-20).
        self.fixer.unchanged = "No code changes\n\nA flaky test.\n\n## Evidence\n- x\n"
        rerun = dict(check("build", "fail"), link="https://github.com/leodip/goiabada/actions/runs/111/job/333")
        lint = dict(check("lint", "pass"), link="https://github.com/leodip/goiabada/actions/runs/111/job/444")
        self.gh.checks = [
            [check("build", "fail"), lint],
            [lint],  # the rerun job is not listed yet
            [lint, dict(rerun, bucket="pending")],
            [lint, rerun],
        ]
        self.assertEqual(self.deliver(), "red")
        self.assertEqual(self.gh.made("pr checks"), 4)
        self.assertEqual([e["outcome"] for e in self.events("ci.result")], ["red", "red"])

    def test_a_fix_the_ci_fixer_names_outside_the_change_reaches_the_follow_ups(self):
        self.fixer.unchanged = ("No code changes\n\nA test took random letters for a link.\n\n## Follow-up\n"
                                "Assert no link on the markup\n\nThirteen tests look for the letters href.\n\n"
                                "## Evidence\n- x\n")
        rerun = dict(check("build", "pass"), link="https://github.com/leodip/goiabada/actions/runs/111/job/333")
        self.gh.checks = [[check("build", "fail")], [rerun]]
        self.assertEqual(self.deliver(), "green")
        drafting = [p for p in self.fixer.prompts if p.startswith("# Draft a fix found outside the change")]
        self.assertEqual(len(drafting), 1)
        self.assertIn("**K-ci-1** Assert no link on the markup", drafting[0])
        # The follow-ups comment was posted before CI; this one comes after it, numbered on.
        self.assertEqual(self.events("followups.continued")[0]["start"], 1)
        self.assertIn("Assert no link on the markup", self.gh.bodies["pr comment last"])

    def test_a_rerun_check_with_a_new_link_counts_though_never_seen_running(self):
        self.fixer.unchanged = "No code changes\n\nA flaky test.\n\n## Evidence\n- x\n"
        rerun = dict(check("build", "pass"), link="https://github.com/leodip/goiabada/actions/runs/111/job/333")
        self.gh.checks = [[check("build", "fail")], [rerun]]
        self.assertEqual(self.deliver(), "green")
        self.assertEqual(self.gh.made("pr checks"), 2)

    def test_a_failed_bot_check_does_not_hold_up_the_rerun_ones(self):
        self.fixer.unchanged = "No code changes\n\nA flaky test, and the bot's.\n\n## Evidence\n- x\n"
        bot = dict(check("bot", "fail"), workflow="", link="https://app.coderabbit.ai/change-stack/leodip/goiabada/pull/450/7")
        rerun = dict(check("build", "fail"), link="https://github.com/leodip/goiabada/actions/runs/111/job/333")
        self.gh.checks = [[check("build", "fail"), bot], [bot, dict(rerun, bucket="pending")], [bot, dict(rerun, bucket="pass")]]
        self.assertEqual(self.deliver(), "red")
        self.assertEqual(self.gh.made("pr checks"), 3)
        self.assertEqual(self.events("ci.result")[1]["failed"], ["bot"])

    def test_the_failed_jobs_run_again_only_once(self):
        self.fixer.unchanged = "No code changes\n\nAn outage.\n\n## Evidence\n- x\n"
        rerun = dict(check("build", "fail"), link="https://github.com/leodip/goiabada/actions/runs/111/job/333")
        self.gh.checks = [[check("build", "fail")], [dict(rerun, bucket="pending")], [rerun]]
        self.assertEqual(self.deliver(), "red")
        self.assertEqual(self.gh.made("run rerun"), 1)
        self.assertEqual(len(self.fixer.prompts), 2)  # the fixer looks at the second failure too
        self.assertEqual([e["outcome"] for e in self.events("ci.result")], ["red", "red"])

    def test_a_failed_check_with_no_actions_run_is_not_run_again(self):
        self.fixer.unchanged = "No code changes\n\nThe bot's.\n\n## Evidence\n- x\n"
        self.gh.checks = [[dict(check("bot", "fail"), link="https://app.coderabbit.ai/change-stack/leodip/goiabada/pull/450/7")]]
        self.assertEqual(self.deliver(), "red")
        self.assertEqual((self.gh.made("run rerun"), len(self.events("ci.rerun"))), (0, 0))

    def test_review_comments_are_answered_once_ci_concludes_and_decide_the_outcome(self):
        from unittest import mock

        from placido import comments

        seen = []

        def answer(ctx, hub, number, branch, ci, outcome, sleep):
            seen.append((number, branch, outcome))
            waits.append(sleep)
            return "red"

        waits = []

        self.gh.checks = [[check("build", "pass")]]
        with mock.patch.object(comments, "answer", answer):
            self.assertEqual(self.deliver(), "red")
        self.assertEqual(seen, [(450, "placido/439-rate-limit", "green")])
        before = self.now
        waits[0](60)  # it waits as CI's wait does
        self.assertEqual(self.now, before + 60)

        # Red CI, after the fixes: the comments are answered all the same (#869).
        self.gh.checks = [[check("build", "fail")]] * 2 + [[check("build", "pending")], [check("build", "fail")]]
        seen.clear()
        with mock.patch.object(comments, "answer", answer):
            self.assertEqual(self.deliver(), "red")
        self.assertEqual(seen, [(450, "placido/439-rate-limit", "red")])

        # CI still running when the wait ends: the reviewers may not be done either.
        self.gh.checks = [[check("build", "pending")]] * 500
        seen.clear()
        with mock.patch.object(comments, "answer", answer):
            self.assertEqual(self.deliver(), "pending")
        self.assertEqual(seen, [])

    def test_no_ci_at_all(self):
        self.assertEqual(self.deliver(), "none")
        self.assertGreaterEqual(self.now, github.ATTACH_SECONDS)

    def test_ci_still_running_when_the_wait_ends(self):
        self.gh.checks = [[check("build", "pending")]] * 500
        self.assertEqual(self.deliver(), "pending")

    def test_the_wait_is_logged_and_the_live_view_says_what_ci_is_still_running(self):
        self.run.echo = io.StringIO()
        self.gh.lag = 3  # polls at 0:00, 0:30 and 1:00 still see the previous head
        running = [check("build", "pass"), check("lint", "fail"), check("unit", "pending"),
                   check("e2e", "pending"), check("race", "pending"), check("docs", "pending")]
        self.gh.checks = [[], [], running, running, [check("build", "pass")]]
        self.assertEqual(self.deliver(), "green")
        wait = self.events("ci.wait")[0]
        self.assertEqual((wait["pr"], wait["limit"]), (450, "1h"))
        self.assertEqual(wait["commit"], sh(self.repo, "git", "rev-parse", "HEAD")[:12])
        lines = [line for line in self.run.echo.getvalue().splitlines() if "…" in line]
        self.assertIn("CI on #450 · waiting for GitHub to move the pull request to ", lines[0])
        self.assertIn("(1:00)", lines[0])
        self.assertIn("CI on #450 · waiting for the checks to start (2:00)", lines[1])
        self.assertIn("CI on #450 · 2 of 6 checks done; running unit, e2e, race, +1 more;"
                      " failed so far: lint (3:00)", lines[2])
        self.assertEqual(len(lines), 3)

    def test_a_resumed_delivery_reuses_the_pull_request_and_issues(self):
        self.gh.checks = [[check("build", "pass")]] * 2
        self.deliver()
        self.deliver()
        self.assertEqual((self.gh.made("pr create"), self.gh.made("pr comment")), (1, 1))
        self.assertEqual(self.gh.made("pr edit"), 4)


class FollowUpsTest(DeliverCase):
    """Every follow-up, drafted by the interview or by the drafting step, goes into one
    comment on the pull request with the command that files it; none is filed. Notes
    for other issues the change affects are posted on them."""

    def setUp(self) -> None:
        super().setUp()
        self.gh.checks = [[check("build", "pass")]] * 2
        issue = self.run.path.parent
        (issue / "followup-remove-chi.md").write_text(  # an older draft, without header lines
            "# refactor: replace chi\n\nDrafted during the specification.\n\n## Why\n\npprof is linked.\n"
        )
        self.run.event("review.followups", items=[
            {"id": "F1", "title": "Expose the limit in metrics", "description": "no gauge yet", "why": "separate feature"},
            {"id": "F2", "title": "Carry the chi draft forward", "description": "", "why": "decision 9"},
        ])
        drafts = self.run.path / "followups"
        (drafts / "notes").mkdir(parents=True)
        (drafts / "F1.md").write_text(
            "# feat: expose the rate limit as a gauge\n\nLabels: `enhancement`, `go`\n"
            "Left out because: metrics are a feature of their own.\n"
            "Searched: `gh issue list --state all --search \"rate limit metrics\"`: no match.\n\n"
            "The limiter counts in memory and nothing exports it.\n\nDone when: a gauge exists.\n"
        )
        (drafts / "F2.md").write_text("Duplicate of followup-remove-chi.md\n")
        (drafts / "notes" / "396.md").write_text("The images no longer need zone data from their base image.\n")
        (drafts / "notes" / "439.md").write_text("The run's own issue gets no note.\n")

    def test_drafts_and_commands_on_the_pull_request(self):
        self.deliver()
        comment = next(argv for argv in self.gh.calls if argv[1:3] == ["pr", "comment"])
        self.assertEqual(comment[3], "450")
        order = [" ".join(argv[1:3]) for argv in self.gh.calls if argv[0] == "gh"]
        self.assertLess(order.index("pr create"), order.index("pr comment"))
        body = self.gh.bodies["pr comment"]
        self.assertIn(f"2 pieces of work left out of {PR}", body)
        self.assertLess(body.index("### 1. refactor: replace chi"), body.index("### 2. feat: expose the rate limit"))
        self.assertNotIn("Carry the chi draft forward", body)  # F2 duplicates the interview's draft
        self.assertIn("Left out because: metrics are a feature of their own.", body)
        self.assertIn("Labels: `enhancement`, `go` · Searched: `gh issue list", body)
        self.assertIn("> The limiter counts in memory and nothing exports it.\n>\n> Done when: a gauge exists.", body)
        bodies = self.run.path / "followups" / "bodies"
        self.assertIn(
            "gh issue create --title 'feat: expose the rate limit as a gauge' --label enhancement --label go"
            f" --body-file {bodies / '2.md'}", body,
        )
        self.assertEqual((bodies / "2.md").read_text(), "The limiter counts in memory and nothing exports it.\n\nDone when: a gauge exists.\n")
        (event,) = self.events("followups.posted")
        self.assertEqual((event["on"], event["count"]), ("pr 450", 2))
        self.assertEqual(self.gh.made("issue create"), 0)

    def test_notes_go_on_the_issues_they_concern(self):
        self.deliver()
        notes = [argv for argv in self.gh.calls if argv[1:3] == ["issue", "comment"]]
        self.assertEqual([argv[3] for argv in notes], ["396"])
        self.assertEqual(self.gh.bodies["issue comment"],
                         f"The images no longer need zone data from their base image.\n\nFrom {PR}.\n")
        self.assertIn("Noted on [#396](https://github.com/leodip/goiabada/issues/396#issuecomment-9001)",
                      self.gh.bodies["pr edit last"])

    def test_the_release_notes_are_a_comment_of_their_own_once(self):
        agreement = self.run.path.parent / "agreement.md"
        agreement.write_text(agreement.read_text().replace(
            "## Decisions", "## Release notes\n- Copy the templates again.\n\n## Decisions"))
        self.deliver()
        comments = [Path(a[a.index("--body-file") + 1]).name for a in self.gh.calls if a[1:3] == ["pr", "comment"]]
        self.assertEqual(comments, ["release-notes-comment.md", "followups-comment.md"])
        self.assertEqual(self.gh.bodies["pr comment"], "## Release notes\n\n- Copy the templates again.\n")
        self.assertIn("[the release notes comment](https://github.com/leodip/goiabada/pull/450#issuecomment-9002)",
                      self.gh.bodies["pr edit last"])
        self.deliver()
        self.assertEqual(self.gh.made("pr comment"), 2)

    def test_a_resumed_delivery_posts_nothing_again(self):
        self.deliver()
        self.deliver()
        self.assertEqual((self.gh.made("pr comment"), self.gh.made("issue comment")), (1, 1))

    def test_nothing_to_post(self):
        for path in self.run.path.parent.glob("followup-*.md"):
            path.unlink()
        for path in (self.run.path / "followups").rglob("*.md"):
            path.unlink()
        self.run.event("review.followups", items=[])
        self.deliver()
        self.assertEqual((self.gh.made("pr comment"), self.gh.made("issue comment")), (0, 0))
        self.assertNotIn("## Follow-ups", self.gh.bodies["pr edit last"])


class FoldedTest(unittest.TestCase):
    def test_only_the_folded_in_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(github.folded_issues(folder), [])
            (folder / "agreement.md").write_text("## Decisions\n- #1\n## Folded in\n- #5 and #7, #5 again\n## Seams\n#9\n")
            self.assertEqual(github.folded_issues(folder), [5, 7])

    def test_only_the_numbers_that_lead_a_bullet(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "agreement.md").write_text(
                "## Folded in\n"
                "- None. #630 built the OTP half and left the issue open.\n"  # #641's
                "- **#5 Greet in Spanish:** all of it, as #3 asked.\n"
                "  Its sibling #6 stays open.\n"
                "- **A stray space in the help:** fixed here, as #8 noticed.\n"
                "- #9: the rest of it, which #10 left.\n"
            )
            self.assertEqual(github.folded_issues(folder), [5, 9])
