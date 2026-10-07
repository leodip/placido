import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from placido import config, decisions, driver, implement, runlog, status
from placido.step import StepResult

SLICES = [
    {"id": 1, "title": "Option parsing", "delivers": "a", "blocked_by": [], "gates": []},
    {"id": 2, "title": "Languages", "delivers": "b", "blocked_by": [1], "gates": []},
    {"id": 3, "title": "GREET_LANG", "delivers": "c", "blocked_by": [2], "gates": []},
]

RESULT = "Build slice {n}\n\nWhat slice {n} changes.\n\n## Evidence\n- ok\n"


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeRunner:
    """Plays the implementer: each attempt follows the next behaviour in the script."""

    def __init__(self, run: runlog.Run, worktree: Path) -> None:
        self.run = run
        self.worktree = worktree
        self.script: list[str] = []  # per attempt: build (default), fail, blocked
        self.attempts: list[str] = []
        self.agents: list[str] = []  # the agent of each attempt
        self.questions: list[str | None] = []  # what each asking attempt's question read as
        self.closed: list[tuple] = []
        self.herdr = FakeHerdr()

    def __call__(self, role, agent, task, name=None, check=None, ask=None, **kwargs) -> StepResult:
        self.attempts.append(name)
        self.agents.append(agent.agent)
        folder = self.run.step_dir(len(self.attempts), name)
        result = folder / "result.md"
        behaviour = self.script.pop(0) if self.script else "build"
        n = name.rsplit("-", 1)[1]
        (self.worktree / f"slice{n}.txt").write_text(f"attempt {len(self.attempts)}\n")
        if behaviour == "fail":
            return StepResult("invalid", folder)
        if behaviour in ("refusal", "quota", "unavailable", "auth", "context"):
            self.run.event("agent.failed", step=folder.name, kind=behaviour, message=f"a {behaviour} message")
            return StepResult(behaviour, folder)
        if behaviour in ("ask", "ask-and-forget"):
            # The agent asks; the user answers in its tab; the agent records the decision
            # (unless it forgets) and finishes, as Step does with a real agent.
            result.write_text("Blocked: which languages?\n")
            self.questions.append(ask(result))
            result.rename(folder / decisions.QUESTION.format(n=1))
            if behaviour == "ask":
                with decisions.path(self.run.path).open("a") as out:
                    out.write(f"## D1 · {folder.name} · 2026-10-02\n**Question:** which?\n"
                              "**Decision:** en and es.\n**Why:** asked.\n")
        result.write_text(RESULT.format(n=n))
        problems = check(result) if check else []
        return StepResult("invalid" if problems else "success", folder)

    def close_agent(self, agent, pane, folder, kind):
        self.closed.append((agent, pane, folder.name, kind))


class FakeHerdr:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.statuses: list[str] = []  # each status placido kept for the run, recorded by DriverTestCase

    def notify(self, title, body, sound="none"):
        self.notes.append(title)


class DriverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "user.email", "t@t")
        (self.repo / "README").write_text("hi\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "init")
        cfg = self.tmp / "config.toml"
        cfg.write_text("[implement]\nmutations = 0\n[limits]\nstage_attempts = 2\n")
        self.settings = config.load(cfg)
        self.run = runlog.Run.create(self.tmp / "runs", "repo", "03-x", {})
        self.run.event("worktree.created", path=str(self.repo), workspace="w1")
        self.runner = FakeRunner(self.run, self.repo)
        shown = mock.patch.object(
            status, "show", side_effect=lambda run_dir, value: self.runner.herdr.statuses.append(value)
        )
        shown.start()
        self.addCleanup(shown.stop)
        self.ctx = driver.Context(
            self.run, self.runner, self.runner.herdr, self.settings,
            config.AgentSpec("claude", "opus", "high"), self.repo, {},
        )

    def events(self, kind: str) -> list[dict]:
        return [e for e in runlog.read_events(self.run.path) if e["event"] == kind]

    def subjects(self) -> list[str]:
        return sh(self.repo, "git", "log", "--format=%s").splitlines()


class DriveTest(DriverTestCase):
    def test_builds_every_slice_in_blocker_order(self):
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.subjects(), ["Build slice 3", "Build slice 2", "Build slice 1", "init"])
        self.assertEqual(implement.committed(self.run.path), {1, 2, 3})
        self.assertEqual(len(self.events("run.slices_done")), 1)

    def test_each_slice_start_says_how_many_slices_there_are(self):
        driver.drive(self.ctx, SLICES)
        self.assertEqual([(e["slice"], e["of"]) for e in self.events("slice.start")], [(1, 3), (2, 3), (3, 3)])

    def test_a_failed_attempt_is_stashed_and_retried(self):
        self.runner.script = ["build", "fail", "build", "build"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.runner.attempts.count("implement-slice-2"), 2)
        stashes = sh(self.repo, "git", "stash", "list")
        self.assertIn("placido: slice 2 attempt failed (invalid", stashes)

    def test_gives_up_after_the_attempt_limit(self):
        self.runner.script = ["build", "fail", "fail"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "gave-up")
        self.assertEqual(implement.committed(self.run.path), {1})
        self.assertEqual(self.events("slice.gave_up")[0]["slice"], 2)
        self.assertIn("placido · 03-x: slice 2 failed 2 times", self.runner.herdr.notes)

    def test_a_question_is_answered_and_the_slice_goes_on(self):
        self.runner.script = ["build", "ask"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.runner.questions, ["which languages?"])
        self.assertEqual(decisions.entries(self.run.path), [(1, "02-implement-slice-2")])

    def test_a_decision_must_be_recorded(self):
        self.runner.script = ["build", "ask-and-forget", "fail"]  # stage_attempts is 2
        self.assertEqual(driver.drive(self.ctx, SLICES), "gave-up")
        self.assertEqual(implement.committed(self.run.path), {1})


class ResumeTest(DriverTestCase):
    def interrupt_slice_2(self) -> None:
        """What a run killed in the middle of slice 2 leaves behind."""

        driver.drive(self.ctx, SLICES[:1])
        self.run.event("slice.start", slice=2, title="Languages")
        self.run.event("step.start", step="02-implement-slice-2", role="implement")
        self.run.event(
            "agent.start", step="02-implement-slice-2", agent="claude", model="opus",
            effort="high", pane="w1:p3", name="implement-w1-p3",
        )
        (self.repo / "half-done.txt").write_text("partial\n")

    def test_detects_the_interrupted_attempt(self):
        self.interrupt_slice_2()
        attempt = driver.interrupted(self.run.path)
        self.assertEqual(attempt["slice"], 2)
        self.assertEqual(attempt["agent"]["name"], "implement-w1-p3")

    def test_resume_stops_the_agent_stashes_and_continues(self):
        self.interrupt_slice_2()
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(
            self.runner.closed, [("implement-w1-p3", "w1:p3", "02-implement-slice-2", "claude")]
        )
        self.assertIn("placido: interrupted slice 2", sh(self.repo, "git", "stash", "list"))
        failed = self.events("slice.failed")[0]
        self.assertEqual(failed["outcome"], "interrupted")
        self.assertEqual(implement.committed(self.run.path), {1, 2, 3})
        self.assertFalse((self.repo / "half-done.txt").exists())

    def test_ctrl_c_stops_the_agent_at_once(self):
        self.interrupt_slice_2()
        driver.stopping(self.ctx, SLICES)
        self.assertEqual(len(self.runner.closed), 1)
        self.assertEqual(self.events("slice.failed")[0]["outcome"], "interrupted")
        self.assertIsNone(driver.interrupted(self.run.path))  # nothing left for the resume
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(len(self.runner.closed), 1)

    def test_interruptions_do_not_use_up_attempts(self):
        driver.drive(self.ctx, SLICES[:1])
        for _ in range(3):
            self.run.event("slice.start", slice=2, title="Languages")
            driver.recover(self.ctx, SLICES)
        self.assertEqual(driver.failures(self.run.path, 2), 0)
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")

    def test_a_finished_interrupted_attempt_is_salvaged_not_redone(self):
        self.interrupt_slice_2()
        folder = self.run.path / "steps" / "02-implement-slice-2"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.md").write_text(RESULT.format(n=2))
        driver.recover(self.ctx, SLICES)
        self.assertEqual(implement.committed(self.run.path), {1, 2})
        self.assertEqual(len(self.events("slice.salvaged")), 1)
        self.assertEqual(self.subjects()[0], "Build slice 2")
        self.assertEqual(sh(self.repo, "git", "stash", "list"), "")

    def test_an_interrupted_attempt_that_fails_its_checks_is_stashed(self):
        self.interrupt_slice_2()
        folder = self.run.path / "steps" / "02-implement-slice-2"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.md").write_text("no evidence here\n")
        driver.recover(self.ctx, SLICES)
        self.assertIn("slice.unsalvaged", [e["event"] for e in runlog.read_events(self.run.path)])
        self.assertEqual(self.events("slice.failed")[0]["outcome"], "interrupted")
        self.assertIn("interrupted slice 2", sh(self.repo, "git", "stash", "list"))

    def test_nothing_to_recover(self):
        driver.drive(self.ctx, SLICES[:1])
        self.assertIsNone(driver.interrupted(self.run.path))
        driver.recover(self.ctx, SLICES)
        self.assertEqual(self.events("slice.failed"), [])

    def test_clean_interruption_has_no_stash(self):
        driver.drive(self.ctx, SLICES[:1])
        self.run.event("slice.start", slice=2, title="Languages")
        driver.recover(self.ctx, SLICES)
        self.assertIsNone(self.events("slice.failed")[0]["stash"])
        self.assertEqual(sh(self.repo, "git", "stash", "list"), "")


class LockTest(DriverTestCase):
    def test_a_second_process_is_refused(self):
        with driver.Lock(self.run.path):
            (self.run.path / "lock").write_text("1\n")  # pid 1 is always alive
            with self.assertRaisesRegex(driver.RunError, "pid 1"):
                with driver.Lock(self.run.path):
                    pass
        self.assertFalse((self.run.path / "lock").exists())

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        (self.run.path / "lock").write_text("999999999\n")
        with driver.Lock(self.run.path):
            self.assertEqual((self.run.path / "lock").read_text().strip(), str(os.getpid()))


if __name__ == "__main__":
    unittest.main()


class FallbackTest(DriverTestCase):
    """The implement chain by default: claude opus, then claude opus at max, which a
    refusal skips as the same model, then pi."""

    def test_a_refusal_moves_to_the_next_agent_and_restarts_the_slice(self):
        self.runner.script = ["build", "refusal", "build", "build"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.runner.agents, ["claude", "claude", "pi", "pi"])
        self.assertIn("placido: slice 2 attempt failed (refusal", sh(self.repo, "git", "stash", "list"))
        (moved,) = self.events("agent.fallback")
        self.assertEqual((moved["role"], moved["reason"], moved["to"]),
                         ("implement", "refusal", "pi deepseek/deepseek-v4.1-flash xhigh"))
        self.assertIn("placido · 03-x: implement moves to pi (deepseek/deepseek-v4.1-flash)", self.runner.herdr.notes)

    def test_refusals_do_not_use_up_attempts(self):
        self.runner.script = ["refusal", "fail", "build", "build", "build"]  # stage_attempts is 2
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")

    def test_every_agent_refusing_stops_the_run(self):
        self.runner.script = ["refusal"] * 2
        self.assertEqual(driver.drive(self.ctx, SLICES), "refused")
        self.assertEqual(self.runner.agents, ["claude", "pi"])
        self.assertEqual(len(self.events("agent.chain_exhausted")), 1)
        self.assertIn("placido · 03-x: implement: every agent refused it", self.runner.herdr.notes)

    def test_a_quota_skips_entries_on_the_same_subscription(self):
        self.ctx.agent = config.AgentSpec("codex", "gpt-6.1-sol", "high")  # then claude, then pi
        self.runner.script = ["quota", "build", "build", "build"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.runner.agents, ["codex", "claude", "claude", "claude"])

    def test_a_resumed_run_stays_on_the_agent_it_moved_to(self):
        self.runner.script = ["refusal", "build"]
        self.assertEqual(driver.drive(self.ctx, SLICES[:1]), "done")
        self.ctx.chain, self.ctx.agent = None, config.AgentSpec("claude", "opus", "high")  # a new process
        self.assertEqual(driver.drive(self.ctx, SLICES[:2]), "done")
        self.assertEqual(self.runner.agents, ["claude", "pi", "pi"])

    def test_a_model_that_cannot_run_moves_on_without_using_an_attempt(self):
        self.ctx.agent = config.AgentSpec("codex", "gpt-6.1-sol", "high")
        self.runner.script = ["unavailable", "fail", "build", "build", "build"]  # stage_attempts is 2
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.runner.agents[:2], ["codex", "claude"])
        self.assertEqual(self.events("agent.fallback")[0]["reason"], "unavailable")

    def test_a_logged_out_agent_stops_the_run(self):
        self.runner.script = ["auth"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "logged-out")
        self.assertIn("placido · 03-x: claude is logged out", self.runner.herdr.notes)

    def test_a_context_overflow_is_retried_once(self):
        self.runner.script = ["context", "build", "build", "build"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "done")
        self.assertEqual(self.runner.agents[:2], ["claude", "claude"])

    def test_a_second_context_overflow_gives_up(self):
        self.runner.script = ["context", "context"]
        self.assertEqual(driver.drive(self.ctx, SLICES), "gave-up")
        self.assertEqual(self.events("slice.gave_up")[0]["reason"], "context")
        self.assertIn("placido · 03-x: slice 1 overflows the context window", self.runner.herdr.notes)


class ManualImplementTest(DriverTestCase):
    """`placido implement` builds one slice by hand, and must agree with `placido run`
    on which agent the issue is on."""

    def test_continues_on_the_agent_the_run_moved_to(self):
        self.runner.script = ["refusal", "build"]
        driver.drive(self.ctx, SLICES[:1])  # claude refused slice 1; pi built it
        fresh = driver.Context(
            self.run, self.runner, self.runner.herdr, self.settings,
            config.AgentSpec("claude", "opus", "high"), self.repo, {},
        )
        self.assertEqual(driver.implement_chain(fresh).current.model, "deepseek/deepseek-v4.1-flash")

    def test_a_refusal_sets_the_work_aside_and_moves_the_run_on(self):
        self.runner.script = ["refusal"]
        self.assertEqual(driver.build_slice(self.ctx, SLICES[0]), "refusal")
        self.assertIsNone(driver.after_failure(self.ctx, 1, "refusal"))
        self.assertIn("placido: slice 1 attempt failed (refusal", sh(self.repo, "git", "stash", "list"))
        self.assertEqual(self.ctx.agent.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(len(self.events("agent.fallback")), 1)  # the next run starts there too

    def test_a_logged_out_agent_says_how_to_stop(self):
        self.runner.script = ["auth"]
        driver.build_slice(self.ctx, SLICES[0])
        self.assertEqual(driver.after_failure(self.ctx, 1, "auth"), "logged-out")


class SidebarTest(DriverTestCase):
    def test_the_status_follows_the_slices(self):
        driver.drive(self.ctx, SLICES[:2])
        self.assertEqual(self.runner.herdr.statuses, ["working · slice 1/2", "working · slice 2/2", "working · 2 slices built"])

    def test_a_stopped_run_says_why(self):
        driver.show_stopped(self.ctx, "refused")
        driver.show_stopped(self.ctx, "something-new")
        self.assertEqual(self.runner.herdr.statuses[-2:], ["stopped · every agent refused", "stopped · something-new"])
