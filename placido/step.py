"""One step of a run: a role's agent in its own Herdr tab, prompted, awaited, and recorded."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from placido import alerts, decisions, outcomes, runlog, status
from placido.config import AgentSpec
from placido.herdr import Herdr, HerdrError

# Decided 2026-10-02: agents run with every approval bypassed, as GBD did, so a run
# never stalls on a permission prompt. Codex also runs its hooks without the review
# it asks for whenever a hook changes, since Herdr's hook reports the session ID that
# finds the transcript. pi has no approval system.
UNATTENDED = {
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
    "pi": [],
}

INSTRUCTION = "Read {prompt} and do what it asks."

FOOTER = """

---

When you have finished, write your result to this file:

{result}

Placido takes that file as the sign that you are done, so write it last.
"""

# Dialog footers. Herdr reported the agent idle under Claude Code's bypass-mode warning
# on 2026-10-02, so placido also reads the screen and never types while one shows.
# Claude Code: "Enter to confirm · Esc to cancel". Codex: "enter continue · esc quit"
# (folder trust), "t trust all · enter review · esc close" (hook review), and
# "k keep & next · esc dismiss & close" (the warnings panel, opened by the user with F2).
DIALOG_MARKERS = ("Enter to confirm", "Esc to cancel", "esc quit", "esc close", "esc dismiss")

@dataclass(frozen=True)
class TrustDialog:
    question: str  # text that identifies the dialog
    yes: str  # the option that trusts the folder
    selected: str  # the mark on the selected option


# Claude Code asks once per new git root, and codex once per repository root, whether
# to trust the folder; no setting skips either. Decided 2026-10-02 (option B): placido
# answers yes itself, but only for these exact questions, and only once the selection
# is seen on the yes option.
TRUST_DIALOGS = (
    TrustDialog("Is this a project you created or one you trust?", "Yes, I trust this folder", "❯"),
    TrustDialog("Trust this folder? Codex can read, edit, and run files here", "Trust and continue", "›"),
)

# Claude Code can run commands in the background and end its turn while they run,
# waking the agent again when they finish. Herdr then reports the agent idle, which
# placido took for "stopped" and nudged, then gave up on (Goiabada #331, 2026-10-02).
# So the screen decides: a turn's footer ("done 6:01 PM · 1 shell, 1 monitor still
# running") or the status bar ("bypass permissions on · 1 shell") naming running
# shells or monitors means the agent is waiting, not done.
FOOTER_LINE = re.compile(r"\bdone \d{1,2}:\d{2}")
RUNNING = re.compile(r"·\s*\d+ (?:shells?|monitors?)\b")
BACKGROUND_LIMIT = 3600  # longest wait on an agent's background work before nudging it
# Quitting with background work running asks first; placido stops the work, so no
# build or test of a finished step goes on in the worktree.
EXIT_BACKGROUND = ("Background work is running", "Exit and stop tasks")


def _missing(error: HerdrError) -> bool:
    """Herdr no longer knows the pane or tab."""

    return error.code.endswith("not_found") or "not found" in str(error)


def background_running(screen: str) -> bool:
    """Whether the agent's screen shows background work still running."""

    lines = screen.splitlines()[-30:]
    footers = [line for line in lines if FOOTER_LINE.search(line)]
    if footers and "still running" in footers[-1] and RUNNING.search(footers[-1]):
        return True
    bars = [line for line in lines if "permissions on" in line]
    return bool(bars and RUNNING.search(bars[-1]))


# Herdr error codes meaning the agent is no longer running in its pane.
GONE = ("agent_not_found", "agent_not_running", "agent_exited")

# How each agent quits by itself, so its session log is complete before its tab closes.
EXIT_COMMANDS = {"claude": "/exit", "codex": "/quit", "pi": "/quit"}
EXIT_SECONDS = 30

SETTLE_SECONDS = 2  # an agent must stay ready this long before placido types into it
PROGRESS_SECONDS = 30  # how often the live view says an agent is still working
POLL_SECONDS = 2

FIX = (
    "Placido checked your output and found problems:\n{problems}\n"
    "Fix them, then write your result to {result} again."
)
MAX_FIXES = 3  # rejected results before the step ends as invalid

NUDGE = (
    "You stopped without writing {result}. Finish the task in {prompt}, "
    "then write your result to {result}."
)

# After a failed turn that waiting can cure, the same session carries on: nothing
# about the work was judged, and the agent keeps its context.
CONTINUE = (
    "Your last turn ended with an error ({error}), which has cleared. Carry on with the "
    "task in {prompt} from where you were, then write your result to {result}."
)
# A transient error reaches placido only after the agent's own retries (claude 10,
# codex 5, pi 3), so the outage is real: pause for minutes, not seconds.
TRANSIENT_PAUSES = (120, 300, 600)
QUOTA_MARGIN = 60  # seconds past a quota's reset before trying again
MAX_QUOTA_WAITS = 2  # a reset time that keeps being wrong must not hold a step forever


class AgentGone(RuntimeError):
    pass


@dataclass(frozen=True)
class StepResult:
    outcome: str  # success; no-result when the agent stopped without writing its result;
    # agent-exited when the agent quit, for example after a dialog was answered "no";
    # invalid when the result kept failing its check; or a failed turn's kind from
    # outcomes.KINDS (refusal, quota, unavailable, transient, auth, context, error)
    path: Path  # the step folder
    agent: str = ""  # the agent's Herdr name and pane, for resuming a kept-open agent
    pane: str = ""
    failure: outcomes.Failure | None = None  # why the agent's turn failed, if it did


def launch_args(spec: AgentSpec) -> list[str]:
    return [*spec.args(), *UNATTENDED[spec.agent]]


def agent_name(role: str, pane_id: str) -> str:
    """A Herdr agent name, unique while the pane lives: implement-w8-p2."""

    return f"{role}-{pane_id.replace(':', '-')}".lower()[:32]


def find_transcript(session: dict[str, Any] | None, home: Path) -> Path | None:
    """The agent's own session log, from the native session Herdr's integration reported."""

    if not session or not session.get("value"):
        return None
    value = str(session["value"])
    if session.get("kind") == "path":
        path = Path(value)
        return path if path.is_file() else None
    patterns = {
        "claude": (home / ".claude" / "projects", f"*/{value}.jsonl"),
        "codex": (home / ".codex" / "sessions", f"**/rollout-*{value}.jsonl"),
        "pi": (home / ".pi" / "agent" / "sessions", f"**/*_{value}.jsonl"),
    }
    folder, pattern = patterns.get(str(session.get("agent")), (None, ""))
    if folder is None:
        return None
    return next(iter(sorted(folder.glob(pattern))), None)


def next_step_number(run: runlog.Run) -> int:
    steps = run.path / "steps"
    return 1 + (len([p for p in steps.iterdir() if p.is_dir()]) if steps.is_dir() else 0)


class Step:
    """Runs one role's agent to completion; every action goes to the event log."""

    def __init__(
        self,
        run: runlog.Run,
        herdr: Herdr,
        workspace_id: str,
        cwd: Path,
        home: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        env: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        quota_wait: int = 2 * 3600,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.env = env or {}
        self.sleep = sleep
        self.quota_wait = quota_wait  # longest wait for a quota reset before giving up the step
        self.wall = wall  # epoch time, to compare with a quota's reset
        self.run = run
        self.herdr = herdr
        self.workspace_id = workspace_id
        self.cwd = cwd
        self.home = home or Path.home()
        self.clock = clock

    def __call__(
        self,
        role: str,
        spec: AgentSpec,
        task: str,
        name: str | None = None,
        interactive: bool = False,
        check: Callable[[Path], list[str]] | None = None,
        keep_open: bool = False,
        ask: Callable[[Path], str | None] | None = None,
    ) -> StepResult:
        """Run the step. An interactive step is a conversation with the user, so an idle
        agent is waiting for them rather than done. check, given the result file,
        returns problems with it, which the agent is asked to fix. ask, given the result
        file, returns the question in it when the agent needs the user's decision: the
        user is brought to the agent's tab, and the step goes on once the agent writes
        its result again. The agent's tab is closed afterwards, once its output is safe,
        unless keep_open."""

        folder, prompt, result = self._new_step(name or role, task)
        began = self.clock()
        self.run.event("step.start", step=folder.name, role=role)

        # PLACIDO_STEP_DIR tells tools the agent runs, such as `placido mutate`, where to record.
        env = {**self.env, "PLACIDO_STEP_DIR": str(folder)}
        pane = self.herdr.create_tab(self.workspace_id, self.cwd, folder.name, env)
        agent = agent_name(role, pane)
        self.run.event(
            "agent.start", step=folder.name, agent=spec.agent, model=spec.model,
            effort=spec.effort, pane=pane, name=agent,
        )
        self._status(status.text(status.WORKING, status.label(folder.name)))
        try:
            self.herdr.start_agent(agent, spec.agent, pane, launch_args(spec))
        except HerdrError as error:
            # A dialog, such as a one-time warning, holds the agent before it is ready;
            # the readiness check before the first prompt gets the user for it.
            if error.code != "agent_not_ready":
                raise
        return self._conduct(
            agent, pane, folder, prompt, result, spec.agent, began, "startup", interactive, check,
            keep_open, ask,
        )

    def resume(
        self,
        role: str,
        kind: str,
        agent: str,
        pane: str,
        task: str,
        name: str | None = None,
        check: Callable[[Path], list[str]] | None = None,
        keep_open: bool = False,
        ask: Callable[[Path], str | None] | None = None,
    ) -> StepResult:
        """A new step with an agent kept open by an earlier one, such as the reviewer in a
        later round, whose resumed session keeps its context and its cache."""

        folder, prompt, result = self._new_step(name or role, task)
        began = self.clock()
        self.run.event("step.start", step=folder.name, role=role, resumed=agent)
        return self._conduct(
            agent, pane, folder, prompt, result, kind, began, "work", False, check, keep_open, ask
        )

    def _new_step(self, name: str, task: str) -> tuple[Path, Path, Path]:
        folder = self.run.step_dir(next_step_number(self.run), name)
        prompt, result = folder / "prompt.md", folder / "result.md"
        prompt.write_text(task.rstrip() + FOOTER.format(result=result), encoding="utf-8")
        return folder, prompt, result

    def _conduct(
        self, agent: str, pane: str, folder: Path, prompt: Path, result: Path, kind: str,
        began: float, phase: str, interactive: bool,
        check: Callable[[Path], list[str]] | None, keep_open: bool,
        ask: Callable[[Path], str | None] | None = None,
    ) -> StepResult:
        failure = None
        try:
            self._work(agent, pane, folder, INSTRUCTION.format(prompt=prompt), phase)
            outcome, failure = self._finish(
                agent, pane, folder, prompt, result, kind, interactive, check, ask
            )
        except AgentGone:
            self.run.event("agent.exited", step=folder.name, name=agent)
            self._notify(f"{agent} exited", f"{folder.name} stopped because the agent quit")
            outcome = "success" if result.is_file() else "agent-exited"

        self._record(agent, pane, folder, kind, keep_open)
        self.run.event(
            "step.end", step=folder.name, outcome=outcome, seconds=round(self.clock() - began, 1)
        )
        if outcome != "success":
            self._status(status.text(status.WORKING, f"{status.label(folder.name)} {outcome.replace('-', ' ')}"))
        return StepResult(outcome, folder, agent, pane, failure)

    def _finish(
        self,
        agent: str,
        pane: str,
        folder: Path,
        prompt: Path,
        result: Path,
        kind: str,
        interactive: bool,
        check: Callable[[Path], list[str]] | None,
        ask: Callable[[Path], str | None] | None = None,
    ) -> tuple[str, outcomes.Failure | None]:
        """Get a result that passes its check: nudge once when it is missing, and send
        the check's problems back to the agent up to MAX_FIXES times. A turn that failed
        is waited out when a pause can cure it, and otherwise ends the step with its kind."""

        nudged, fixes, retries, quota_waits = False, 0, 0, 0
        while True:
            if not result.is_file():
                if interactive:
                    self._converse(agent, pane, folder, result)
                else:
                    self._ready(agent, pane, folder, "work")  # make sure it has really stopped
            if not result.is_file() and not interactive:
                failure = self._failure(agent, pane, folder, kind)
                if failure is not None:
                    pause = None
                    if failure.kind == "transient" and retries < len(TRANSIENT_PAUSES):
                        pause, retries = TRANSIENT_PAUSES[retries], retries + 1
                    elif failure.kind == "quota" and quota_waits < MAX_QUOTA_WAITS:
                        pause = self._quota_pause(failure)
                        quota_waits += pause is not None
                    if pause is None:
                        self.run.event(
                            "agent.failed", step=folder.name, name=agent, kind=failure.kind,
                            message=failure.message, resets_at=failure.resets_at,
                        )
                        return failure.kind, failure
                    self._wait_out(agent, folder, failure, pause)
                    self._work(agent, pane, folder, CONTINUE.format(
                        error=failure.message[:200], prompt=prompt, result=result,
                    ), "work")
                    continue
            if not result.is_file():
                if nudged:
                    return "no-result", None
                nudged = True
                self.run.event("agent.nudge", step=folder.name, reason="no result file")
                self._work(agent, pane, folder, NUDGE.format(result=result, prompt=prompt), "work")
                continue
            question = ask(result) if ask else None
            if question is not None:
                self._question(agent, pane, folder, result, question)
                continue
            problems = check(result) if check else []
            if not problems:
                return "success", None
            fixes += 1
            self.run.event("result.rejected", step=folder.name, problems=problems)
            if fixes > MAX_FIXES:
                return "invalid", None
            result.unlink()
            listed = "\n".join(f"- {problem}" for problem in problems)
            self._work(agent, pane, folder, FIX.format(problems=listed, result=result), "work")

    def _failure(self, agent: str, pane: str, folder: Path, kind: str) -> outcomes.Failure | None:
        """Why the agent's last turn failed, from its transcript, or from its screen
        when the transcript cannot be found."""

        now = datetime.fromtimestamp(self.wall()).astimezone()
        transcript = find_transcript(self._known_session(folder) or self._session(agent, folder), self.home)
        if transcript is not None:
            return outcomes.last_failure(transcript, kind, now)
        return outcomes.screen_failure(self._screen(pane, source="visible"), now)

    def _quota_pause(self, failure: outcomes.Failure) -> float | None:
        """Seconds to wait for a quota to reset, or None when the reset is unknown or
        further away than quota_wait."""

        if failure.resets_at is None:
            return None
        pause = max(0.0, failure.resets_at - self.wall()) + QUOTA_MARGIN
        return pause if pause <= self.quota_wait + QUOTA_MARGIN else None

    def _wait_out(self, agent: str, folder: Path, failure: outcomes.Failure, pause: float) -> None:
        until = time.strftime("%H:%M", time.localtime(self.wall() + pause))
        if failure.kind == "quota":
            self.run.event("agent.quota_wait", step=folder.name, name=agent, seconds=round(pause),
                           resets_at=failure.resets_at, message=failure.message)
            self._notify(f"{folder.name} waits for quota", f"{failure.message[:120]} · trying again at {until}")
            self._status(status.text(status.WAITING, f"quota until {until}"))
        else:
            self.run.event("agent.retry", step=folder.name, name=agent, kind=failure.kind,
                           seconds=round(pause), message=failure.message)
            self._status(status.text(status.WAITING, f"{failure.kind} error, retry at {until}"))
        self.sleep(pause)
        self._status(status.text(status.WORKING, status.label(folder.name)))

    def _question(self, agent: str, pane: str, folder: Path, result: Path, question: str) -> None:
        """Bring the user to the agent's tab for its question, keeping the question in the
        step folder, and wait while they talk until the agent writes its result again."""

        kept = folder / decisions.QUESTION.format(n=decisions.asked(folder) + 1)
        result.rename(kept)
        self.run.event("agent.question", step=folder.name, name=agent, question=question, file=kept.name)
        self._notify(f"{folder.name} asks you", f"{question[:160]} · answer in the {folder.name} tab")
        self._status(status.text(status.YOU, f"answer in {status.label(folder.name)}"))
        self._converse(agent, pane, folder, result, told=True)
        self.run.event("agent.answered", step=folder.name, name=agent)

    def _converse(self, agent: str, pane: str, folder: Path, result: Path, told: bool = False) -> None:
        """Wait out a conversation with the user until the result appears. An idle agent
        without a result is waiting for the user's answer. told: the user was already
        notified."""

        while not result.is_file():
            state = self._call(self.herdr.wait, agent, ("idle", "done", "blocked"))
            if result.is_file():
                return
            if state == "blocked" or self._dialog(pane):
                if not self._answer_known(pane, folder):
                    self._needs_user(agent, pane, folder, "work")
                continue
            self._status(status.text(status.YOU, f"answer in {status.label(folder.name)}"))
            if not told:
                # Only Herdr's notification: the user started this conversation and is
                # at hand, so an email for every interview question is noise (2026-10-07).
                self._notify(f"{folder.name} needs you", f"answer in the {folder.name} tab", mail=False)
                self.run.event("agent.waiting", step=folder.name, name=agent)
                told = True
            self._call(self.herdr.wait, agent, ("working", "blocked"))
            self._status(status.text(status.WORKING, status.label(folder.name)))

    def _work(self, agent: str, pane: str, folder: Path, text: str, phase: str) -> None:
        """Prompt the agent once it is truly ready, then wait until it settles."""

        self._ready(agent, pane, folder, phase)
        self._status(status.text(status.WORKING, status.label(folder.name)))
        self._call(self.herdr.prompt, agent, text)
        # Only states the prompt leads to: Herdr returns at once when the current state
        # matches, so waiting for idle here returned before the agent had even started.
        try:
            self._call(self.herdr.wait, agent, ("working", "blocked"), timeout=60)
        except HerdrError as error:
            if error.code != "timeout":
                raise
        # Recorded as soon as the agent works, while Herdr knows it: once the agent
        # exits, Herdr forgets it, and an interrupted step still needs its transcript.
        self._session(agent, folder)
        self._ready(agent, pane, folder, "work", settle=False)

    def _ready(self, agent: str, pane: str, folder: Path, phase: str, settle: bool = True) -> None:
        """Return once the agent is idle with no dialog showing, getting the user for any
        dialog. Herdr's status alone is not enough: it once reported idle under a dialog,
        and the prompt's Enter answered it."""

        began = self.clock()
        background: float | None = None  # when the agent was first seen waiting on background work
        while True:
            status = self._status_of(agent)
            if status in ("working", "unknown"):
                try:
                    self._call(
                        self.herdr.wait, agent, ("idle", "done", "blocked"), timeout=PROGRESS_SECONDS
                    )
                except HerdrError as error:
                    if error.code != "timeout":
                        raise
                    self._progress(folder, agent, self.clock() - began)
                continue
            if status == "blocked" or self._dialog(pane):
                if not self._answer_known(pane, folder):
                    self._needs_user(agent, pane, folder, phase)
                continue
            # Waited the limit out, or the result says it is done whatever still runs: no wait.
            waiting = not (background is not None and background < 0) and not (folder / "result.md").is_file()
            if waiting and background_running(self._screen(pane, source="visible")):
                if background is None:
                    background = self.clock()
                    self.run.event("agent.background", step=folder.name, name=agent)
                if self.clock() - background < BACKGROUND_LIMIT:
                    self._await_background(agent, folder, began)
                    continue
                self.run.event("agent.background_limit", step=folder.name, name=agent,
                               seconds=round(self.clock() - background))
                background = -1.0
            elif background is not None and background >= 0:
                self.run.event("agent.background_done", step=folder.name, name=agent,
                               seconds=round(self.clock() - background, 1))
                background = None
            if not settle:
                return
            self.sleep(SETTLE_SECONDS)
            if self._status_of(agent) in ("idle", "done") and not self._dialog(pane):
                return

    def _await_background(self, agent: str, folder: Path, began: float) -> None:
        """Wait for an agent idle on its background work to be woken by it."""

        try:
            self._call(self.herdr.wait, agent, ("working", "blocked"), timeout=PROGRESS_SECONDS)
        except HerdrError as error:
            if error.code != "timeout":
                raise
            self._progress(folder, agent, self.clock() - began, "waiting on its background work")

    def _answer_known(self, pane: str, folder: Path) -> bool:
        """Answer an agent's folder-trust question with yes; False when the screen shows
        anything else, so the user is asked instead."""

        for _ in range(3):
            screen = self._screen(pane, source="visible")
            dialog = next((d for d in TRUST_DIALOGS if d.question in screen), None)
            if dialog is None:
                return False
            selected = next((line for line in screen.splitlines() if dialog.selected in line), "")
            if dialog.yes in selected:
                self._call(self.herdr.send_keys, pane, "enter")
                self.run.event("agent.trusted", step=folder.name, pane=pane)
                self.sleep(POLL_SECONDS)
                return True
            self._call(self.herdr.send_keys, pane, "down")
            self.sleep(0.5)
        return False

    def _progress(self, folder: Path, agent: str, seconds: float, doing: str = "working") -> None:
        """A live-view line only, not an event: the log records what happened, not waiting."""

        if self.run.echo is None:
            return
        minutes, secs = divmod(int(seconds), 60)
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp}  …             {folder.name} · {agent} {doing} ({minutes}:{secs:02d})",
              file=self.run.echo, flush=True)

    def _needs_user(self, agent: str, pane: str, folder: Path, phase: str) -> None:
        (folder / f"blocked-{phase}.txt").write_text(self._screen(pane), encoding="utf-8")
        self.run.event("agent.blocked", step=folder.name, name=agent, phase=phase)
        self._status(status.text(status.YOU, f"dialog in {status.label(folder.name)}"))
        self._notify(f"{agent} needs you", f"{folder.name} is waiting on a question")
        while self._status_of(agent) == "blocked" or self._dialog(pane):
            self.sleep(POLL_SECONDS)
        self.run.event("agent.unblocked", step=folder.name, name=agent)

    def _status_of(self, agent: str) -> str:
        return self._call(self.herdr.agent, agent).status

    def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call Herdr about the agent, turning "the agent is gone" into AgentGone."""

        try:
            return method(*args, **kwargs)
        except HerdrError as error:
            if error.code in GONE:
                raise AgentGone(str(error)) from None
            raise

    def _dialog(self, pane: str) -> bool:
        screen = self._screen(pane, source="visible")
        tail = "\n".join(screen.splitlines()[-15:])
        return any(marker in tail for marker in DIALOG_MARKERS)

    def _notify(self, title: str, body: str, mail: bool = True) -> None:
        """Notify the user, naming the issue, since several can be in flight at once."""

        alerts.notify(self.run, self.herdr, title, body, mail=mail)

    def close_agent(self, agent: str, pane: str, folder: Path, kind: str) -> None:
        """Stop an agent left running, such as one from an interrupted run, keeping its
        screen and transcript in its step folder before its tab closes."""

        folder.mkdir(parents=True, exist_ok=True)
        try:
            working = self.herdr.agent(agent).status == "working"
        except HerdrError:
            working = False
        if working:
            # Interrupt its turn first, so it cannot go on changing the worktree.
            self.run.event("agent.interrupted", step=folder.name, name=agent)
            self.herdr.send_keys(pane, "esc")
            try:
                self.herdr.wait(agent, ("idle", "done", "blocked"), timeout=30)
            except HerdrError as error:
                if error.code not in GONE:
                    raise
        self._record(agent, pane, folder, kind, keep_open=False)

    def _record(self, agent: str, pane: str, folder: Path, kind: str, keep_open: bool) -> None:
        """Save the screen and the transcript, then close the tab, but only once the agent
        has exited and the copies are verified; otherwise keep the tab as evidence."""

        try:
            (folder / "screen.txt").write_text(self.herdr.read(pane), encoding="utf-8")
        except HerdrError as error:
            # The pane is gone, as when a resumed run tidies an attempt whose tab was
            # already closed: keep the screen saved back then.
            if not (folder / "screen.txt").is_file():
                (folder / "screen.txt").write_text(f"(screen unavailable: {error})\n", encoding="utf-8")
        session = self._session(agent, folder) or self._known_session(folder)
        if keep_open:
            self._copy_transcript(session, folder)
            return
        exited = self._exit(agent, kind, pane)
        copied = self._copy_transcript(session, folder)
        if not exited:
            self._keep_tab(folder, pane, "the agent did not exit")
        elif not copied:
            self._keep_tab(folder, pane, "the transcript was not copied")
        else:
            try:
                tab = self.herdr.tab_of(pane)
                self.herdr.close_tab(tab)
                self.run.event("tab.closed", step=folder.name, tab=tab)
            except HerdrError as error:
                if _missing(error):  # closed already, by placido or the user: nothing to keep
                    self.run.event("tab.gone", step=folder.name, pane=pane)
                else:
                    self._keep_tab(folder, pane, f"closing failed: {error}")

    def _session(self, agent: str, folder: Path) -> dict[str, Any] | None:
        try:
            session = self.herdr.agent(agent).session
        except HerdrError as error:
            if error.code not in GONE:
                self.run.event("herdr.warning", error=str(error))
            return None
        known = self._known_session(folder)
        if session and (known is None or known.get("value") != session.get("value")):
            self.run.event(
                "agent.session", step=folder.name, agent=session.get("agent"),
                kind=session.get("kind"), value=session.get("value"),
            )
        return session

    def _known_session(self, folder: Path) -> dict[str, Any] | None:
        """The session logged earlier for this step, for when the agent is already gone."""

        found = None
        for event in runlog.read_events(self.run.path):
            if event.get("event") == "agent.session" and event.get("step") == folder.name:
                found = {key: event.get(key) for key in ("agent", "kind", "value")}
        return found

    def _exit(self, agent: str, kind: str, pane: str = "") -> bool:
        """Ask the agent to quit and wait until it has; True once it is gone."""

        if self._gone(agent):
            return True
        try:
            self.herdr.prompt(agent, EXIT_COMMANDS[kind])
        except HerdrError as error:
            if error.code in GONE:
                return True
            self.run.event("herdr.warning", error=str(error))
            return False
        answered = False
        for _ in range(EXIT_SECONDS):
            self.sleep(1)
            if self._gone(agent):
                self.run.event("agent.exited", name=agent, by="placido")
                return True
            if not answered and pane and self._stop_background(agent, pane):
                answered = True
        return False

    def _stop_background(self, agent: str, pane: str) -> bool:
        """Answer "Exit and stop tasks" when quitting asks about background work; True if
        it did. Only with the selection seen on that option."""

        screen = self._screen(pane, source="visible")
        question, option = EXIT_BACKGROUND
        selected = next((line for line in screen.splitlines() if "❯" in line), "")
        if question not in screen or option not in selected:
            return False
        self.herdr.send_keys(pane, "enter")
        self.run.event("agent.background_stopped", name=agent)
        return True

    def _gone(self, agent: str) -> bool:
        try:
            self.herdr.agent(agent)
        except HerdrError as error:
            return error.code in GONE
        return False

    def _copy_transcript(self, session: dict[str, Any] | None, folder: Path) -> bool:
        transcript = find_transcript(session, self.home)
        if transcript is None:
            self.run.event("transcript.missing", step=folder.name)
            return False
        copy = folder / "transcript.jsonl"
        if copy.is_file() and copy.stat().st_size == transcript.stat().st_size > 0:
            return True  # copied when the step ended, and nothing was added since
        shutil.copyfile(transcript, copy)
        size = copy.stat().st_size
        if size == 0 or size != transcript.stat().st_size:
            self.run.event("transcript.unverified", step=folder.name, source=str(transcript))
            return False
        self.run.event("transcript.copied", step=folder.name, source=str(transcript), bytes=size)
        return True

    def _keep_tab(self, folder: Path, pane: str, reason: str) -> None:
        self.run.event("tab.kept", step=folder.name, pane=pane, reason=reason)
        self._notify(f"kept the {folder.name} tab", reason)

    def _screen(self, pane: str, source: str = "recent-unwrapped") -> str:
        try:
            return self.herdr.read(pane, source=source)
        except HerdrError as error:
            return f"(screen unavailable: {error})\n"

    def status(self, text: str) -> None:
        """Keep the run's status, for `placido status`."""

        self._status(text)

    def _status(self, text: str) -> None:
        status.show(self.run.path, text)
