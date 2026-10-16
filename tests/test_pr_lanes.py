import json
import threading
import time
from pathlib import Path
from unittest import mock

from placido import close, pr, pragents, runlog
from placido.herdr import Herdr
from tests.test_pr import CONFIG, PRTestCase, sh


def wait_for(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class LaneTestCase(PRTestCase):
    """Reviewers in lanes of their own: the context can make a runner for a lane."""

    def setUp(self) -> None:
        super().setUp()
        self.log = self.tmp / "commands.log"
        self.ctx = self.lane_context()

    def lane_context(self) -> pr.Context:
        ctx = self.context()
        ctx.make_runner = self.runner.lane
        ctx.env = {"PLACIDO_NAME": "proj-pr-7", "LOG": str(self.log)}
        return ctx

    def with_setup(self, extra: str = "") -> None:
        self.settings = self.load(
            CONFIG.replace('base = "main"', 'base = "main"\nsetup = "echo $PLACIDO_NAME >> $LOG"\n'
                           'teardown = "echo down $PLACIDO_NAME >> $LOG"') + extra
        )
        self.ctx = self.lane_context()

    def calls(self, name: str) -> list[dict]:
        return [c for c in self.runner.calls if c["name"] == name]


class LanesTest(LaneTestCase):
    def test_each_reviewer_works_in_a_lane_of_its_own(self):
        rnd = pragents.review(self.ctx)
        lane = self.run.path / "lanes" / "b"
        a, b = self.calls("r1-review-a")[0], self.calls("r1-review-b")[0]
        self.assertEqual((a["cwd"], b["cwd"]), (self.worktree, lane))
        self.assertEqual(b["env"]["PLACIDO_NAME"], "proj-pr-7-b")
        self.assertEqual(b["env"]["PLACIDO_WORKTREE"], str(lane))
        self.assertIn(f"Your worktree, {lane}, is at its head", b["task"])
        self.assertIn(f"Your worktree, {self.worktree}, is at its head", a["task"])
        self.assertEqual(sh(lane, "git", "rev-parse", "HEAD"), rnd.head)
        self.assertEqual(runlog.last_event(self.run.path, "pr.lane")["name"], "proj-pr-7-b")
        self.assertEqual(self.calls("r1-crosscheck-b")[0]["cwd"], lane)

    def test_the_reviewers_run_at_once(self):
        b_started = threading.Event()
        seen: list[bool] = []
        self.runner.during["r1-review-a"] = lambda wt: seen.append(b_started.wait(5))
        self.runner.during["r1-review-b"] = lambda wt: b_started.set()
        pragents.review(self.ctx)
        self.assertEqual(seen, [True], "reviewer B started while reviewer A was still at work")

    def test_one_after_the_other_when_parallel_is_off(self):
        self.settings = self.load(CONFIG + "[pr]\nparallel = false\n")
        self.ctx = self.lane_context()
        order: list[str] = []
        self.runner.during["r1-review-a"] = lambda wt: order.append("a")
        self.runner.during["r1-review-b"] = lambda wt: order.append("b")
        pragents.review(self.ctx)
        self.assertEqual(order, ["a", "b"])
        self.assertEqual(self.calls("r1-review-b")[0]["cwd"], self.worktree)
        self.assertFalse((self.run.path / "lanes").exists())

    def test_every_lane_comes_up_while_the_scan_reads(self):
        self.with_setup()
        seen: list[bool] = []
        lines = lambda: len(self.log.read_text().splitlines()) if self.log.exists() else 0  # noqa: E731
        self.runner.during["r1-scan"] = lambda wt: seen.append(wait_for(lambda: lines() == 2))
        pragents.review(self.ctx)
        self.assertEqual(seen, [True], "both stacks came up while the scan was reading")
        self.assertEqual(sorted(self.log.read_text().split()), ["proj-pr-7", "proj-pr-7-b"])
        self.assertTrue((self.run.path / "setup-b.log").is_file())

    def test_a_host_path_change_is_approved_before_any_setup(self):
        self.with_setup()
        self.push_new_head({".placido/stack.sh": "echo changed\n"}, "main")
        self.ctx.pr = self.hub.pull
        self.answers = [True]
        pragents.review(self.ctx)
        kinds = self.kinds()
        self.assertLess(kinds.index("pr.trust_approved"), kinds.index("setup.start"))
        self.assertLess(kinds.index("pr.scan"), kinds.index("setup.start"))

    def test_close_tears_every_lane_down_and_records_the_review(self):
        self.with_setup()
        pragents.review(self.ctx)
        lane = self.run.path / "lanes" / "b"
        with mock.patch.dict("os.environ", {"LOG": str(self.log)}):
            close.close(self.run, Herdr(self.fake), self.settings, force=False)
        self.assertFalse(lane.exists())
        self.assertIn("down proj-pr-7-b", self.log.read_text())
        self.assertNotIn(str(lane), sh(self.repo, "git", "worktree", "list"))
        end = runlog.last_event(self.run.path, "run.end")
        self.assertEqual(end["outcome"], "reviewed")


class JudgeTest(LaneTestCase):
    def test_the_judge_is_told_what_the_cross_checks_settled(self):
        pragents.review(self.ctx)
        task = self.calls("r1-judge")[0]["task"]
        self.assertIn("**Settled by the cross-checks:** A1, B1.", task)
        self.assertIn("**Still open:** none.", task)

    def test_a_refuted_finding_is_left_to_the_judge(self):
        original = self.runner._crosscheck

        def refute_b1(folder: Path, kind: str) -> None:
            original(folder, kind)
            data = json.loads((folder / "crosscheck.json").read_text())
            for verdict in data["verdicts"]:
                if verdict["id"] == "B1":
                    verdict["verdict"] = "refuted"
            (folder / "crosscheck.json").write_text(json.dumps(data))

        self.runner._crosscheck = refute_b1
        pragents.review(self.ctx)
        task = self.calls("r1-judge")[0]["task"]
        self.assertIn("**Settled by the cross-checks:** A1.", task)
        self.assertIn("**Still open:** B1. Refuted, unsure, or checked by nobody", task)


class RenderTest(LaneTestCase):
    def test_the_agents_write_json_and_placido_renders_it(self):
        rnd = pragents.review(self.ctx)
        review = runlog.last_event(self.run.path, "pr.reviewed")
        text = (self.run.path / "steps" / review["step"] / "review.md").read_text()
        self.assertIn("# Reviewer B: 1 finding(s)", text)
        self.assertIn("### B1. problem B1", text)
        final = (rnd.folder / "final.md").read_text()
        self.assertTrue(final.startswith("# Verdict: fix-here\n\nIt greets, nearly."))
        self.assertIn("## Already there before this change (for the maintainer only)", final)
        for call in self.runner.calls:
            if call["name"].startswith(("r1-review", "r1-judge")):
                self.assertNotIn("review.md", call["task"].split("**Write:**")[-1], call["name"])
                self.assertNotIn("final.md", call["task"].split("**Write:**")[-1], call["name"])


class OrphanTest(PRTestCase):
    def test_an_agent_a_stopped_run_left_running_is_closed(self):
        self.run.event("agent.start", step="09-r1-review-a", agent="claude", name="pr-review-w9-p1", pane="w9:p1")
        pragents.review(self.ctx)
        self.assertIn("pr-review-w9-p1", self.runner.closed)
        self.assertEqual(runlog.last_event(self.run.path, "pr.orphan_closed")["name"], "pr-review-w9-p1")
        pragents.review(self.ctx)
        self.assertEqual(self.runner.closed.count("pr-review-w9-p1"), 1)


class EventLogTest(PRTestCase):
    def test_events_from_threads_at_once_stay_whole_lines(self):
        class SlowEcho:
            """A live view that yields on every write, as a terminal under load may, so
            threads that do not take turns interleave their lines."""

            def __init__(self) -> None:
                self.text = ""

            def write(self, part: str) -> None:
                self.text += part
                time.sleep(0.0005)

            def flush(self) -> None:
                pass

        echo = SlowEcho()
        run = runlog.Run(self.run.path, echo=echo)

        def write(n: int) -> None:
            for i in range(25):
                run.event("load", thread=n, i=i)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = (self.run.path / "events.jsonl").read_text().splitlines()
        self.assertEqual(len([json.loads(line) for line in lines if '"load"' in line]), 200)
        # Each event a line of its own in the live view, in the log's order.
        shown = echo.text.split("\n")[:-1]
        logged = [json.loads(line) for line in lines if '"load"' in line]
        self.assertEqual(len(shown), 200)
        for line, event in zip(shown, logged):
            self.assertIn(f"thread={event['thread']} i={event['i']}", line)

    def test_step_folders_taken_at_once_never_share_a_number(self):
        folders: list[Path] = []
        lock = threading.Lock()

        def take(n: int) -> None:
            folder = self.run.new_step_dir(f"x-{n}")
            with lock:
                folders.append(folder)

        threads = [threading.Thread(target=take, args=(n,)) for n in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        numbers = [f.name.split("-", 1)[0] for f in folders]
        self.assertEqual(len(set(numbers)), 20)
