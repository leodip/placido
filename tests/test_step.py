import io
import json
import time
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, runlog, status, step
from placido.config import AgentSpec
from placido.herdr import AgentInfo, HerdrError

CLAUDE = AgentSpec("claude", "opus", "high")


DIALOG = "WARNING: Bypass Permissions mode\n> No, exit\n  Yes, I accept\nEnter to confirm · Esc to cancel\n"


class FakeHerdr:
    """Plays Herdr, the agent, and the user. Each prompt runs the next scripted
    behaviour; each sleep is a moment in which the user answers an open dialog."""

    def __init__(self, result: Path | None = None) -> None:
        self.result = result  # where the agent writes when a behaviour says so
        self.behaviours: list[str] = ["write"]  # per prompt: write, idle, block, or exit
        self.status = "idle"
        self.dialog = False  # a dialog is on screen
        self.alive = True
        self.answer = "yes"  # how the user answers a dialog
        self.write_after_answer = False
        self.startup_dialog: str | None = None  # status Herdr reports under it: blocked or idle
        self.working_timeout = False
        self.session: dict | None = {"agent": "claude", "kind": "id", "value": "abc-123"}
        self.calls: list[tuple] = []
        self.trust: int | None = None  # the trust question is up, its selection at 0 (no) or 1 (yes)
        self.asked = False
        self.pending: str | None = None  # the behaviour of the turn in progress
        self.starting = False
        self.codex = False  # the trust question is codex's: yes first, marked with ›
        self.notice = False  # codex's startup warning panel is showing
        self.questions = 0  # questions an interviewing agent asks before writing its result
        self.refuses_exit = False  # the agent ignores its exit command
        self.working_rounds = 0  # waits that time out while the agent keeps working
        self.transcript: Path | None = None  # where a failing turn writes its error
        self.errors: dict[str, str] = {}  # behaviour name -> the API error text it writes
        self.error_screen: str | None = None  # what the screen shows after a failed turn
        self.background = 0  # waits the agent spends idle on background work before waking
        self.exit_question = False  # quitting asks about the background work still running
        self.pane_gone = False  # the agent's tab was closed already

    def _alive(self):
        if not self.alive:
            raise HerdrError("agent not found", "agent_not_found")

    def create_tab(self, workspace_id, cwd, label, env=None):
        self.calls.append(("tab", workspace_id, str(cwd), label))
        self.tab_env = env
        return "w8:p2"

    def start_agent(self, name, kind, pane_id, args):
        self.calls.append(("start", name, kind, pane_id, args))
        if self.startup_dialog == "trust":
            self.trust, self.dialog, self.status = 0, True, "blocked"
            raise HerdrError("agent is blocked during startup", "agent_not_ready")
        if self.startup_dialog:
            self.dialog, self.status = True, self.startup_dialog
            if self.startup_dialog == "blocked":
                raise HerdrError("agent is blocked during startup", "agent_not_ready")

    def prompt(self, name, text):
        self._alive()
        if text in step.EXIT_COMMANDS.values():
            self.calls.append(("exit", text))
            if self.background:  # Claude Code asks before stopping background work
                self.exit_question = True
                return
            self.alive = self.refuses_exit
            return
        self.calls.append(("prompt", name, text))
        if self.dialog:  # the prompt's Enter answers the dialog with its default, "No, exit"
            self.dialog, self.alive = False, False
            return
        self.pending = self.behaviours.pop(0) if self.behaviours else "idle"
        self.starting = True  # submitted, but Herdr still sees the agent idle for a moment

    def _finish(self):
        """The agent's turn ends: what the current behaviour does happens now."""

        behaviour, self.pending = self.pending, None
        self.status = "idle"
        if behaviour == "write":
            self.result.write_text("done\n")
        elif behaviour == "block":
            self.status, self.dialog, self.write_after_answer = "blocked", True, True
        elif behaviour == "exit":
            self.alive = False
        elif behaviour == "background":  # ends its turn while its builds run, then wakes
            self.background = 2
        elif behaviour == "write-background":  # done, but a monitor it started still runs
            self.result.write_text("done\n")
            self.background = 99
        elif behaviour in self.errors:  # the turn fails with an API error
            text = self.errors[behaviour]
            refused = "safeguards" in text
            kind = "rate_limit" if "hit your" in text else "invalid_request" if refused else "server_error"
            entry = {"type": "assistant", "isApiErrorMessage": True, "error": kind,
                     "message": {"content": [{"type": "text", "text": text}],
                                 "stop_reason": "refusal" if refused else None}}
            with self.transcript.open("a") as out:
                out.write(json.dumps(entry) + "\n")
        elif behaviour == "ask":  # writes a question, then waits for the user in its tab
            self.result.write_text("Blocked: which store should logout clear?\n\nTwo exist.\n")
            self.pending, self.questions = "interview", 1
        elif behaviour == "interview":
            if self.questions:  # asks its next question and waits for the user
                self.pending = "interview"
            else:
                self.result.write_text("done\n")

    def wait(self, name, until, timeout=None):
        self.calls.append(("wait", until))
        self._alive()
        if self.starting:
            if "idle" in until:
                return "idle"  # the wait matches the state the agent is still in
            self.starting, self.status = False, "working"
        if self.working_timeout and "working" in until:
            self.working_timeout = False
            self._finish()
            raise HerdrError("timed out", "timeout")
        if self.status == "idle" and self.background and not self.exit_question and until == ("working", "blocked"):
            self.background -= 1
            if self.background:
                raise HerdrError("timed out", "timeout")
            self.status, self.pending = "working", "write"  # its work finished and woke it
            return self.status
        if self.status == "idle" and self.pending == "interview" and until == ("working", "blocked"):
            self.questions -= 1  # the user answers in the agent's tab
            self.status = "working"
            return self.status
        if self.status == "working" and "working" not in until:
            if self.working_rounds:
                self.working_rounds -= 1
                raise HerdrError("timed out", "timeout")
            self._finish()
            self._alive()
        if self.status not in until:
            raise HerdrError(f"timed out waiting for {until} in {self.status}", "timeout")
        return self.status

    def agent(self, name):
        self._alive()
        if self.starting:
            self.starting, self.status = False, "working"  # work shows up by the next look
        return AgentInfo(self.status, self.session)

    def read(self, pane_id, source="recent-unwrapped", lines=200):
        if self.pane_gone:
            raise HerdrError(f"herdr pane read: pane {pane_id} not found", "pane_not_found")
        if self.notice:
            return (
                "Warnings · 1 of 1 · Startup\nRunning without the shared background server: "
                "command-line configuration overrides (-c) requires embedded mode.\n"
                "k keep & next · esc dismiss & close · ctrl+o copy\n"
            )
        if self.trust is not None and self.codex:
            marks = ["›" if self.trust == i else " " for i in (0, 1)]
            return (
                "Folder access\nTrust this folder? Codex can read, edit, and run files here, subject to\n"
                f"{marks[0]} 1. Trust and continue\n{marks[1]} 2. Quit\n\nenter continue · esc quit\n"
            )
        if self.trust is not None:
            marks = ["❯" if self.trust == i else " " for i in (0, 1)]
            return (
                " Quick safety check: Is this a project you created or one you trust? (Like your own code)\n"
                f" {marks[0]} No, exit\n   {marks[1]} Yes, I trust this folder\n"
                " Enter to confirm · Esc to cancel\n"
            )
        if self.dialog:
            return DIALOG
        if self.exit_question:
            return (
                "Background work is running\nThe following will stop when you exit:\n\n"
                "shell · S=/tmp/claude/scratchpad; for g in vet unit; do …\n\n"
                " ❯ 1. Exit and stop tasks\n   2. Move to background and exit\n   3. Stay\n\n"
                "Enter to confirm · Esc to cancel\n"
            )
        if self.background:
            return (
                "● The gates are running; I'll write the result when they finish.\n\n"
                "✻ Brewed for 2m 10s · done 6:01 PM · 1 shell, 1 monitor still running\n\n"
                "❯ \n  ⏵⏵ bypass permissions on · 1 shell · ← for agents\n"
            )
        return self.error_screen or "screen text\n"

    def send_keys(self, pane_id, *keys):
        self.calls.append(("keys", keys))
        if keys == ("esc",) and self.status == "working":
            self.status = "idle"  # the agent's turn is interrupted
            return
        if keys == ("enter",) and self.exit_question:
            self.exit_question, self.background, self.alive = False, 0, False  # stops its tasks and quits
            return
        for key in keys:
            if key == "down":
                self.trust = min(1, self.trust + 1)
            elif key == "enter" and self.trust is not None:
                yes, self.trust = self.trust == (0 if self.codex else 1), None
                self.dialog, self.status, self.alive = False, "idle", yes

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        if self.asked and (self.dialog or self.notice or self.status == "blocked"):
            self.asked = False
            self.dialog, self.status, self.trust, self.notice = False, "idle", None, False
            if self.answer == "no":
                self.alive = False
            elif self.write_after_answer:
                self.result.write_text("done\n")

    def tab_of(self, pane_id):
        if self.pane_gone:
            raise HerdrError(f"herdr pane get: pane {pane_id} not found", "pane_not_found")
        return "w8:t2"

    def close_tab(self, tab_id):
        self.calls.append(("close", tab_id))

    def notify(self, title, body, sound="none"):
        self.calls.append(("notify", title))
        self.asked = True  # the user answers only once placido has asked them

    def prompts(self):
        return [c[2] for c in self.calls if c[0] == "prompt"]


class StepTestCase(unittest.TestCase):
    now = 1_790_000_000.0  # the wall clock, in epoch seconds

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.run = runlog.Run.create(self.tmp / "runs", "proj", "01-x", {})
        self.fake = FakeHerdr(self.run.path / "steps" / "01-implement" / "result.md")
        transcripts = self.home / ".claude" / "projects" / "-wt"
        transcripts.mkdir(parents=True)
        (transcripts / "abc-123.jsonl").write_text('{"type":"user"}\n')
        self.fake.transcript = transcripts / "abc-123.jsonl"
        # Each status placido keeps for the run is recorded among the fake's calls, in order.
        shown = mock.patch.object(status, "show", side_effect=lambda run_dir, value: self.fake.calls.append(("status", value)))
        shown.start()
        self.addCleanup(shown.stop)

    def step(self, task: str = "Summarize the issue.", **kwargs) -> step.StepResult:
        runner = step.Step(
            self.run, self.fake, "w8", self.tmp / "wt", home=self.home,
            env={"PLACIDO_NAME": "proj-1"}, sleep=self.fake.sleep,
            wall=lambda: self.now, quota_wait=2 * 3600,
        )
        return runner("implement", CLAUDE, task, **kwargs)

    def events(self) -> list[str]:
        return [e["event"] for e in runlog.read_events(self.run.path)]


class StepTest(StepTestCase):
    def test_success_records_everything(self):
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.path, self.run.path / "steps" / "01-implement")
        self.assertEqual(
            self.events(),
            ["step.start", "agent.start", "agent.session", "agent.exited", "transcript.copied",
             "tab.closed", "step.end"],
        )
        self.assertIn(("exit", "/exit"), self.fake.calls)
        self.assertIn(("close", "w8:t2"), self.fake.calls)
        session = runlog.last_event(self.run.path, "agent.session")
        self.assertEqual((session["agent"], session["value"]), ("claude", "abc-123"))
        self.assertEqual((result.path / "transcript.jsonl").read_text(), '{"type":"user"}\n')
        self.assertEqual((result.path / "screen.txt").read_text(), "screen text\n")
        end = runlog.last_event(self.run.path, "step.end")
        self.assertEqual(end["outcome"], "success")

    def test_prompt_file_holds_the_task_and_where_to_write(self):
        result = self.step("Summarize the issue.")
        text = (result.path / "prompt.md").read_text()
        self.assertTrue(text.startswith("Summarize the issue.\n\n---\n"))
        self.assertIn(str(result.path / "result.md"), text)
        prompt = next(c for c in self.fake.calls if c[0] == "prompt")
        self.assertEqual(prompt[2], f"Read {result.path / 'prompt.md'} and do what it asks.")

    def test_agent_runs_in_a_new_tab_with_bypassed_approvals(self):
        self.step()
        self.assertIn(("tab", "w8", str(self.tmp / "wt"), "01-implement"), self.fake.calls)
        start = next(c for c in self.fake.calls if c[0] == "start")
        self.assertEqual(start[1:4], ("implement-w8-p2", "claude", "w8:p2"))
        self.assertEqual(start[4], ["--model", "opus", "--effort", "high", "--dangerously-skip-permissions"])

    def test_the_tab_gets_the_run_variables(self):
        self.step()
        self.assertEqual(
            self.fake.tab_env,
            {"PLACIDO_NAME": "proj-1", "PLACIDO_STEP_DIR": str(self.run.path / "steps" / "01-implement")},
        )

    def test_agent_start_event_names_model_and_effort(self):
        self.step()
        start = runlog.last_event(self.run.path, "agent.start")
        self.assertEqual(
            {k: start[k] for k in ("agent", "model", "effort", "pane", "name")},
            {"agent": "claude", "model": "opus", "effort": "high", "pane": "w8:p2", "name": "implement-w8-p2"},
        )

    def test_types_only_after_the_agent_stays_ready(self):
        self.step()
        until_exit = self.fake.calls[: self.fake.calls.index(("exit", "/exit"))]
        kinds = [c[0] for c in until_exit if c[0] in ("sleep", "prompt", "wait")]
        self.assertEqual(kinds, ["sleep", "prompt", "wait", "wait"])

    def test_waits_for_the_turn_to_start_then_to_end(self):
        # 2026-10-02: waiting for "working or idle" returned at once on the still-idle
        # agent, and placido nudged an agent that was in the middle of its work.
        self.step()
        waits = [c[1] for c in self.fake.calls if c[0] == "wait"]
        self.assertEqual(waits, [("working", "blocked"), ("idle", "done", "blocked")])
        self.assertEqual(len(self.fake.prompts()), 1)
        self.assertNotIn("agent.nudge", self.events())

    def test_sidebar_follows_the_step(self):
        self.step()
        statuses = [c[1] for c in self.fake.calls if c[0] == "status"]
        self.assertEqual(
            statuses, ["working · implement", "working · implement"]
        )

    def test_second_step_gets_the_next_number(self):
        self.step()
        self.fake.result = self.run.path / "steps" / "02-implement" / "result.md"
        self.fake.behaviours = ["write"]
        self.assertEqual(self.step().path.name, "02-implement")


class MissingResultTest(StepTestCase):
    def test_one_nudge_recovers_a_missing_result(self):
        self.fake.behaviours = ["idle", "write"]
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertIn("agent.nudge", self.events())
        self.assertTrue(self.fake.prompts()[1].startswith("You stopped without writing"))

    def test_still_missing_after_the_nudge(self):
        self.fake.behaviours = ["idle", "idle"]
        result = self.step()
        self.assertEqual(result.outcome, "no-result")
        self.assertEqual(runlog.last_event(self.run.path, "step.end")["outcome"], "no-result")


class BackgroundTest(StepTestCase):
    """Claude Code ends its turn while its background commands run and wakes when they
    finish; placido waits rather than nudge (Goiabada #331)."""

    def test_an_agent_waiting_on_background_work_is_not_nudged(self):
        out = io.StringIO()
        self.run.echo = out
        self.fake.behaviours = ["background"]
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertNotIn("agent.nudge", self.events())
        self.assertEqual(len(self.fake.prompts()), 1)
        events = self.events()
        self.assertLess(events.index("agent.background"), events.index("agent.background_done"))
        self.assertIn("waiting on its background work (", out.getvalue())

    def test_the_wait_has_a_limit_then_the_nudge_comes(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
            self.fake.sleep(seconds)
        def wait(name, until, timeout=None):
            clock[0] += timeout or 0
            raise HerdrError("timed out", "timeout")
        self.fake.behaviours = ["background", "write"]
        self.fake.background = 0
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home,
                           clock=lambda: clock[0], sleep=sleep, wall=lambda: self.now)
        real_wait = self.fake.wait
        def waiting(name, until, timeout=None):
            if self.fake.background and until == ("working", "blocked"):
                self.fake.background = 99  # never wakes on its own
                return wait(name, until, timeout)
            return real_wait(name, until, timeout)
        self.fake.wait = waiting
        result = runner("implement", CLAUDE, "Build it.")
        self.assertEqual(result.outcome, "success")
        self.assertIn("agent.background_limit", self.events())
        self.assertIn("agent.nudge", self.events())
        self.assertGreaterEqual(clock[0], step.BACKGROUND_LIMIT)

    def test_quitting_stops_the_background_work_and_the_tab_closes(self):
        self.fake.behaviours = ["write-background"]
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertIn(("keys", ("enter",)), self.fake.calls)
        self.assertIn("agent.background_stopped", self.events())
        self.assertIn("tab.closed", self.events())


class BackgroundScreenTest(unittest.TestCase):
    def test_seen_on_live_screens(self):
        for screen in (
            "✻ Brewed for 2m 10s · done 6:01 PM · 1 shell, 1 monitor still running\n",
            "✻ Cooked for 9s · done 6:01 PM · 1 shell, 1 monitor still running\n\n❯ \n",
            "❯ \n  ⏵⏵ bypass permissions on · 1 shell · ← for agents\n",
            "  ⏵⏵ bypass permissions on · 2 shells\n",
        ):
            with self.subTest(screen=screen):
                self.assertTrue(step.background_running(screen))

    def test_finished_work_does_not_count(self):
        for screen in (
            "✻ Sautéed for 3m 9s · done 5:56 PM\n\n❯ \n  ⏵⏵ bypass permissions on\n",
            # an older footer still on screen, above the newest one
            "✻ Brewed · done 6:01 PM · 1 shell still running\n● Done.\n✻ Cooked · done 6:04 PM\n",
            "● I ran 2 shells in the background earlier; both are done.\n",
        ):
            with self.subTest(screen=screen):
                self.assertFalse(step.background_running(screen))


class DialogMarkerTest(unittest.TestCase):
    def test_known_footers_count_as_dialogs(self):
        for footer in (
            "Enter to confirm · Esc to cancel",
            "enter continue · esc quit",
            "t trust all · enter review · esc close",
            "k keep & next · esc dismiss & close · ctrl+o copy",
        ):
            with self.subTest(footer=footer):
                self.assertTrue(any(m in footer for m in step.DIALOG_MARKERS))


class DialogTest(StepTestCase):
    def test_startup_dialog_waits_for_the_user(self):
        self.fake.startup_dialog = "blocked"
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertEqual(
            self.events()[:4], ["step.start", "agent.start", "agent.blocked", "agent.unblocked"]
        )
        self.assertEqual(runlog.last_event(self.run.path, "agent.blocked")["phase"], "startup")
        self.assertIn("Enter to confirm", (result.path / "blocked-startup.txt").read_text())
        self.assertIn(("notify", "placido · 01-x: implement-w8-p2 needs you"), self.fake.calls)
        self.assertIn(("status", "you · dialog in implement"), self.fake.calls)

    def test_dialog_reported_idle_is_never_typed_into(self):
        # 2026-10-02: Herdr reported idle under Claude Code's bypass warning, the prompt's
        # Enter chose "No, exit", and the nudge was typed into the bare shell.
        self.fake.startup_dialog = "idle"
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertIn("agent.blocked", self.events())
        self.assertEqual(len(self.fake.prompts()), 1)

    def test_dialog_answered_no_ends_the_step_without_typing(self):
        self.fake.startup_dialog = "blocked"
        self.fake.answer = "no"
        result = self.step()
        self.assertEqual(result.outcome, "agent-exited")
        self.assertEqual(self.fake.prompts(), [])
        self.assertIn("agent.exited", self.events())
        self.assertIn(("notify", "placido · 01-x: implement-w8-p2 exited"), self.fake.calls)
        self.assertIn(("status", "working · implement agent exited"), self.fake.calls)

    def test_agent_exiting_mid_work_ends_the_step(self):
        self.fake.behaviours = ["exit"]
        result = self.step()
        self.assertEqual(result.outcome, "agent-exited")
        self.assertEqual(len(self.fake.prompts()), 1)
        self.assertIn("transcript.copied", self.events())  # its session was logged while it worked

    def test_trust_question_is_answered_yes_without_the_user(self):
        self.fake.startup_dialog = "trust"
        result = self.step()
        self.assertEqual(result.outcome, "success")
        keys = [c[1] for c in self.fake.calls if c[0] == "keys"]
        self.assertEqual(keys, [("down",), ("enter",)])
        self.assertIn("agent.trusted", self.events())
        self.assertNotIn("agent.blocked", self.events())
        self.assertFalse(any(c[0] == "notify" for c in self.fake.calls))
        self.assertEqual(len(self.fake.prompts()), 1)

    def test_codex_trust_question_is_answered_too(self):
        self.fake.startup_dialog = "trust"
        self.fake.codex = True
        result = self.step()
        self.assertEqual(result.outcome, "success")
        keys = [c[1] for c in self.fake.calls if c[0] == "keys"]
        self.assertEqual(keys, [("enter",)])  # "Trust and continue" is selected from the start
        self.assertIn("agent.trusted", self.events())

    def test_open_warnings_panel_waits_for_the_user(self):
        # Codex's warnings panel only opens when the user presses F2; placido must not
        # type into it, and leaves closing it to the user.
        self.fake.startup_dialog = "idle"
        self.fake.notice = True
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertEqual([c for c in self.fake.calls if c[0] == "keys"], [])
        self.assertIn("agent.blocked", self.events())
        self.assertEqual(len(self.fake.prompts()), 1)

    def test_trust_answer_needs_the_selection_on_yes(self):
        self.fake.startup_dialog = "trust"
        original = self.fake.send_keys

        def stuck(pane_id, *keys):  # the selection never moves, so yes is never seen
            self.fake.calls.append(("keys", keys))
            if keys == ("enter",):
                original(pane_id, *keys)

        self.fake.send_keys = stuck
        result = self.step()
        self.assertNotIn(("keys", ("enter",)), self.fake.calls)
        self.assertNotIn("agent.trusted", self.events())
        self.assertIn("agent.blocked", self.events())  # the user is asked instead
        self.assertEqual(result.outcome, "success")

    def test_question_during_work_waits_for_the_user(self):
        self.fake.behaviours = ["block"]
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertEqual(runlog.last_event(self.run.path, "agent.blocked")["phase"], "work")
        self.assertTrue((result.path / "blocked-work.txt").is_file())
        self.assertEqual(len(self.fake.prompts()), 1)

    def test_work_that_starts_unseen_still_completes(self):
        self.fake.working_timeout = True
        self.assertEqual(self.step().outcome, "success")

    def test_other_startup_errors_propagate(self):
        def broken(*args):
            raise HerdrError("pane is busy", "pane_not_available")

        self.fake.start_agent = broken
        with self.assertRaisesRegex(HerdrError, "pane is busy"):
            self.step()


class TabTest(StepTestCase):
    def closed(self) -> bool:
        return any(c[0] == "close" for c in self.fake.calls)

    def test_agent_that_will_not_exit_keeps_its_tab(self):
        self.fake.refuses_exit = True
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertFalse(self.closed())
        kept = runlog.last_event(self.run.path, "tab.kept")
        self.assertEqual(kept["reason"], "the agent did not exit")
        self.assertIn(("notify", "placido · 01-x: kept the 01-implement tab"), self.fake.calls)

    def test_missing_transcript_keeps_the_tab(self):
        self.fake.session = {"agent": "claude", "kind": "id", "value": "nowhere"}
        self.step()
        self.assertFalse(self.closed())
        self.assertEqual(runlog.last_event(self.run.path, "tab.kept")["reason"], "the transcript was not copied")

    def test_copy_that_does_not_match_keeps_the_tab(self):
        original = step.shutil.copyfile

        def short_copy(source, target):
            original(source, target)
            Path(target).write_text("")

        with mock.patch.object(step.shutil, "copyfile", short_copy):
            self.step()
        self.assertFalse(self.closed())
        self.assertIn("transcript.unverified", self.events())

    def test_keep_open_leaves_the_agent_running(self):
        self.fake.result = self.run.path / "steps" / "01-review" / "result.md"
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home, sleep=self.fake.sleep)
        result = runner("review", CLAUDE, "Review.", keep_open=True)
        self.assertEqual(result.outcome, "success")
        self.assertNotIn(("exit", "/exit"), self.fake.calls)
        self.assertFalse(self.closed())
        self.assertTrue((result.path / "transcript.jsonl").is_file())

    def test_an_unchanged_transcript_is_copied_once(self):
        self.fake.result = self.run.path / "steps" / "01-review" / "result.md"
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home, sleep=self.fake.sleep)
        result = runner("review", CLAUDE, "Review.", keep_open=True)
        runner.close_agent(result.agent, result.pane, result.path, "claude")
        self.assertEqual(self.events().count("transcript.copied"), 1)
        self.assertIn(("close", "w8:t2"), self.fake.calls)

    def test_closing_a_working_agent_interrupts_it_first(self):
        self.fake.status = "working"
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home, sleep=self.fake.sleep)
        runner.close_agent("implement-w8-p2", "w8:p2", self.run.path / "steps" / "02-implement-slice-2", "claude")
        keys = [c for c in self.fake.calls if c[0] in ("keys", "exit", "close")]
        self.assertEqual(keys, [("keys", ("esc",)), ("exit", "/exit"), ("close", "w8:t2")])
        self.assertIn("agent.interrupted", self.events())
        self.assertTrue((self.run.path / "steps" / "02-implement-slice-2" / "transcript.jsonl").is_file())

    def test_long_work_prints_progress_without_logging_it(self):
        out = io.StringIO()
        self.run.echo = out
        self.fake.working_rounds = 2  # two waits time out before the turn ends
        self.step()
        progress = [line for line in out.getvalue().splitlines() if "working (" in line]
        self.assertEqual(len(progress), 2)
        self.assertIn("01-implement · implement-w8-p2 working (", progress[0])
        self.assertNotIn("working (", (self.run.path / "events.jsonl").read_text())

    def test_session_is_logged_once_while_the_agent_works(self):
        self.fake.behaviours = ["exit"]  # the agent dies mid-turn, after starting work
        self.step()
        self.assertEqual(self.events().count("agent.session"), 1)
        self.assertLess(self.events().index("agent.session"), self.events().index("agent.exited"))

    def test_session_is_known_even_after_the_agent_has_gone(self):
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home, sleep=self.fake.sleep)
        folder = self.run.path / "steps" / "01-implement"
        self.run.event("agent.session", step="01-implement", agent="claude", kind="id", value="abc-123")
        self.fake.alive = False
        runner.close_agent("implement-w8-p2", "w8:p2", folder, "claude")
        self.assertTrue((folder / "transcript.jsonl").is_file())
        self.assertIn(("close", "w8:t2"), self.fake.calls)

    def test_resume_talks_to_the_open_agent_without_a_new_tab(self):
        self.fake.result = self.run.path / "steps" / "01-review-2" / "result.md"
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home, sleep=self.fake.sleep)
        result = runner.resume("review", "codex", "review-w8-p2", "w8:p2", "Verify.", name="review-2", keep_open=True)
        self.assertEqual(result.outcome, "success")
        self.assertEqual((result.agent, result.pane), ("review-w8-p2", "w8:p2"))
        self.assertFalse(any(c[0] in ("tab", "start") for c in self.fake.calls))
        self.assertEqual(runlog.last_event(self.run.path, "step.start")["resumed"], "review-w8-p2")
        self.assertFalse(any(c[0] in ("exit", "close") for c in self.fake.calls))

    def test_exit_command_per_agent(self):
        self.assertEqual(step.EXIT_COMMANDS, {"claude": "/exit", "codex": "/quit", "pi": "/quit"})


class GoneTabTest(StepTestCase):
    """A resumed run tidies an attempt whose agent and tab are gone already: the screen
    saved back then stays, and there is no tab to keep (Goiabada #331)."""

    def test_nothing_is_overwritten_or_kept(self):
        folder = self.run.path / "steps" / "04-implement-slice-2"
        folder.mkdir(parents=True)
        (folder / "screen.txt").write_text("the screen when the attempt stopped\n")
        self.run.event("agent.session", step=folder.name, agent="claude", kind="id", value="abc-123")
        self.fake.alive, self.fake.pane_gone = False, True
        runner = step.Step(self.run, self.fake, "w8", self.tmp / "wt", home=self.home, sleep=self.fake.sleep)
        runner.close_agent("implement-w8-p5", "w8:p5", folder, "claude")
        self.assertEqual((folder / "screen.txt").read_text(), "the screen when the attempt stopped\n")
        self.assertIn("tab.gone", self.events())
        self.assertNotIn("tab.kept", self.events())
        self.assertFalse(any(c[0] == "notify" for c in self.fake.calls))


class InteractiveTest(StepTestCase):
    def test_interview_waits_for_each_answer_until_the_result(self):
        self.fake.behaviours = ["interview"]
        self.fake.questions = 3
        with mock.patch.object(step.alerts, "email") as email:
            result = self.step(interactive=True)
        email.assert_not_called()  # the user started the interview: Herdr's notification will do
        self.assertEqual(result.outcome, "success")
        self.assertEqual(self.fake.questions, 0)
        self.assertNotIn("agent.nudge", self.events())
        self.assertEqual(self.events().count("agent.waiting"), 1)
        notes = [c[1] for c in self.fake.calls if c[0] == "notify"]
        self.assertEqual(notes, ["placido · 01-x: 01-implement needs you"])  # told once, not per question
        self.assertIn(("status", "you · answer in implement"), self.fake.calls)
        self.assertEqual(len(self.fake.prompts()), 1)

    def test_interview_without_questions_finishes_at_once(self):
        self.fake.behaviours = ["interview"]
        result = self.step(interactive=True)
        self.assertEqual(result.outcome, "success")
        self.assertNotIn("agent.waiting", self.events())


class CheckTest(StepTestCase):
    def test_problems_go_back_to_the_agent_until_fixed(self):
        self.fake.behaviours = ["write", "write"]
        answers = [["slice 2 is blocked by 3"], []]
        result = self.step(check=lambda result: answers.pop(0))
        self.assertEqual(result.outcome, "success")
        rejected = runlog.last_event(self.run.path, "result.rejected")
        self.assertEqual(rejected["problems"], ["slice 2 is blocked by 3"])
        fix = self.fake.prompts()[1]
        self.assertIn("- slice 2 is blocked by 3", fix)
        self.assertIn(str(result.path / "result.md"), fix)

    def test_a_result_that_never_passes_is_invalid(self):
        self.fake.behaviours = ["write"] * 10
        result = self.step(check=lambda result: ["always wrong"])
        self.assertEqual(result.outcome, "invalid")
        self.assertEqual(self.events().count("result.rejected"), step.MAX_FIXES + 1)
        self.assertEqual(len(self.fake.prompts()), step.MAX_FIXES + 1)


class TranscriptTest(StepTestCase):
    def test_missing_session_is_logged(self):
        self.fake.session = None
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertIn("transcript.missing", self.events())
        self.assertNotIn("agent.session", self.events())

    def test_finds_each_agent_log(self):
        codex = self.home / ".codex" / "sessions" / "2026" / "10" / "02"
        codex.mkdir(parents=True)
        (codex / "rollout-2026-10-02T05-34-20-cid.jsonl").write_text("")
        pi = self.home / ".pi" / "agent" / "sessions" / "--wt--"
        pi.mkdir(parents=True)
        (pi / "2026-10-02T08-34-27-355Z_pid.jsonl").write_text("")
        cases = {
            "claude": ({"agent": "claude", "kind": "id", "value": "abc-123"}, "abc-123.jsonl"),
            "codex": ({"agent": "codex", "kind": "id", "value": "cid"}, "rollout-2026-10-02T05-34-20-cid.jsonl"),
            "pi": ({"agent": "pi", "kind": "id", "value": "pid"}, "2026-10-02T08-34-27-355Z_pid.jsonl"),
        }
        for agent, (session, name) in cases.items():
            with self.subTest(agent=agent):
                self.assertEqual(step.find_transcript(session, self.home).name, name)

    def test_path_sessions_are_used_directly(self):
        path = self.home / "s.jsonl"
        path.write_text("")
        self.assertEqual(step.find_transcript({"agent": "pi", "kind": "path", "value": str(path)}, self.home), path)
        self.assertIsNone(step.find_transcript({"agent": "pi", "kind": "path", "value": "/gone"}, self.home))

    def test_unknown_session(self):
        self.assertIsNone(step.find_transcript({"agent": "claude", "kind": "id", "value": "nope"}, self.home))
        self.assertIsNone(step.find_transcript(None, self.home))


class NamesTest(unittest.TestCase):
    def test_agent_name(self):
        self.assertEqual(step.agent_name("review", "w12:p3"), "review-w12-p3")

    def test_launch_args(self):
        self.assertEqual(
            step.launch_args(AgentSpec("codex", "gpt-6.1-sol", "max", "fast")),
            ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=max", "-c", "service_tier=fast",
             "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        )
        self.assertEqual(
            step.launch_args(AgentSpec("pi", "a/b", "low")),
            ["--provider", "openrouter", "--model", "a/b", "--thinking", "low"],
        )


class ActiveRunTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self, project: str, issue: str, *events: str) -> runlog.Run:
        run = runlog.Run.create(self.root, project, issue, {})
        worktree = self.root / "worktrees" / issue
        worktree.mkdir(parents=True)
        for kind in events:
            run.event(kind)
            if kind == "run.start":
                run.event("worktree.created", path=str(worktree))
        return run

    def test_newest_ready_unclosed_run_of_the_project(self):
        ready = self.make("proj", "01-a", "run.start", "run.ready")
        self.make("proj", "02-b", "run.start", "run.ready", "run.end", "run.closed")
        self.make("other", "03-c", "run.start", "run.ready")
        self.make("proj", "04-d", "run.start")
        self.assertEqual(runlog.active_run(self.root, "proj"), ready.path)

    def test_none(self):
        self.assertIsNone(runlog.active_run(self.root, "proj"))


class StepCommandTest(unittest.TestCase):
    def test_without_an_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "proj"
            repo.mkdir()
            import subprocess

            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            err = io.StringIO()
            with mock.patch.dict("os.environ", {"PLACIDO_RUNS_DIR": str(Path(tmp) / "runs")}), \
                    mock.patch.object(Path, "cwd", return_value=repo), \
                    mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
                code = cli.main(["step", "implement", "say", "hi"])
        self.assertEqual(code, 1)
        self.assertIn("no active run for proj", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class FailedTurnTest(StepTestCase):
    """A turn that ends in an API error is waited out when a pause can cure it, and
    otherwise ends the step with its kind, rather than being nudged."""

    def limit(self, minutes: int) -> str:
        when = time.strftime("%-I:%M %p", time.localtime(self.now + minutes * 60))
        return f"You've hit your session limit · resets {when}"

    def sleeps(self) -> list[float]:
        return [c[1] for c in self.fake.calls if c[0] == "sleep" and c[1] > 10]

    def test_a_refusal_ends_the_step_without_a_nudge(self):
        self.fake.errors = {"refuse": "API Error: Opus's safeguards flagged this message"}
        self.fake.behaviours = ["refuse"]
        result = self.step()
        self.assertEqual(result.outcome, "refusal")
        self.assertIn("safeguards", result.failure.message)
        self.assertNotIn("agent.nudge", self.events())
        failed = runlog.last_event(self.run.path, "agent.failed")
        self.assertEqual(failed["kind"], "refusal")
        self.assertEqual(runlog.last_event(self.run.path, "step.end")["outcome"], "refusal")

    def test_a_transient_error_is_retried_in_the_same_session(self):
        self.fake.errors = {"blip": "API Error: Repeated 529 Overloaded errors"}
        self.fake.behaviours = ["blip", "write"]
        result = self.step()
        self.assertEqual(result.outcome, "success")
        self.assertEqual(self.sleeps(), [step.TRANSIENT_PAUSES[0]])
        self.assertIn("Your last turn ended with an error (API Error: Repeated 529", self.fake.prompts()[1])
        self.assertEqual(self.events().count("agent.retry"), 1)
        self.assertEqual([c for c in self.fake.calls if c[0] == "start"].__len__(), 1)

    def test_transient_errors_give_up_after_the_pauses(self):
        self.fake.errors = {"blip": "API Error: Repeated 529 Overloaded errors"}
        self.fake.behaviours = ["blip"] * 4
        result = self.step()
        self.assertEqual(result.outcome, "transient")
        self.assertEqual(self.sleeps(), list(step.TRANSIENT_PAUSES))

    def test_a_quota_within_the_wait_is_waited_out(self):
        self.fake.errors = {"limit": self.limit(90)}
        self.fake.behaviours = ["limit", "write"]
        result = self.step()
        self.assertEqual(result.outcome, "success")
        (pause,) = self.sleeps()
        self.assertAlmostEqual(pause, 90 * 60 + step.QUOTA_MARGIN, delta=60)
        waited = runlog.last_event(self.run.path, "agent.quota_wait")
        self.assertIn("session limit", waited["message"])
        self.assertIn("waits for quota", [c[1] for c in self.fake.calls if c[0] == "notify"][0])

    def test_a_quota_beyond_the_wait_ends_the_step(self):
        self.fake.errors = {"limit": "You've hit your weekly limit · resets Oct 9, 3pm"}
        self.fake.behaviours = ["limit"]
        result = self.step()
        self.assertEqual(result.outcome, "quota")
        self.assertIsNotNone(result.failure.resets_at)
        self.assertEqual(self.sleeps(), [])

    def test_a_quota_with_no_reset_time_ends_the_step(self):
        self.fake.errors = {"limit": "You've hit your usage limit"}
        self.fake.behaviours = ["limit"]
        self.assertEqual(self.step().outcome, "quota")

    def test_a_quota_that_keeps_coming_back_is_not_waited_forever(self):
        self.fake.errors = {"limit": self.limit(30)}
        self.fake.behaviours = ["limit"] * 3
        self.assertEqual(self.step().outcome, "quota")
        self.assertEqual(len(self.sleeps()), step.MAX_QUOTA_WAITS)

    def test_the_screen_is_the_backup_without_a_transcript(self):
        self.fake.session = None
        self.fake.behaviours = ["idle"]
        self.fake.error_screen = "• Working\n■ This request has been flagged for possible cybersecurity risk.\n› \n"
        result = self.step()
        self.assertEqual(result.outcome, "refusal")

    def test_a_plain_stop_is_still_nudged(self):
        self.fake.behaviours = ["idle", "write"]
        self.assertEqual(self.step().outcome, "success")
        self.assertIn("agent.nudge", self.events())
        self.assertNotIn("agent.failed", self.events())


class QuestionTest(StepTestCase):
    def test_a_question_brings_the_user_to_the_tab_and_the_step_goes_on(self):
        self.fake.behaviours = ["ask"]
        result = self.step(ask=lambda path: path.read_text().split("\n")[0][9:] if
                           path.read_text().startswith("Blocked:") else None)
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.path.joinpath("result.md").read_text(), "done\n")
        self.assertTrue(result.path.joinpath("question-1.md").read_text().startswith("Blocked: which store"))
        asked = runlog.last_event(self.run.path, "agent.question")
        self.assertEqual(asked["question"], "which store should logout clear?")
        self.assertIn("agent.answered", self.events())
        notes = [c[1] for c in self.fake.calls if c[0] == "notify"]
        self.assertEqual(notes, ["placido · 01-x: 01-implement asks you"])
        self.assertIn(("status", "you · answer in implement"), self.fake.calls)
        self.assertNotIn("agent.nudge", self.events())
