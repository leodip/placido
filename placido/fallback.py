"""The fallback chain: which agent a role moves to when its agent refuses or runs out
of quota, and which agent it is on now.

A role's chain is its own agent followed by its `[[fallback]]` entries. A refusal
moves to the next entry at once. A quota that would take longer than `quota_wait`
to reset moves to the next entry on a different subscription, since the same
subscription is out of quota too. Once moved, the role stays on the new entry: the
same content would likely be refused again. Every move is an `agent.fallback` event,
so a resumed run picks up on the agent it had moved to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from placido import runlog
from placido.config import AgentSpec, Config

SWITCH = ("refusal", "quota", "unavailable")  # failures another agent may get past
WHAT = {"refusal": "refused it", "quota": "is out of quota", "unavailable": "cannot run for this login"}

# Whose quota each agent spends: claude and codex run on their subscriptions, and
# pi always names OpenRouter.
SUBSCRIPTION = {"claude": "anthropic", "codex": "chatgpt", "pi": "openrouter"}


class Chain:
    """A role's agents in fallback order, positioned on the one in use."""

    def __init__(
        self,
        run: runlog.Run,
        role: str,
        entries: tuple[AgentSpec, ...],
        notify: Callable[[str, str], None],
        since: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        """since, given each logged event in order, says where this chain's history
        starts, such as the start of a review; without it, the whole run counts."""

        self.run, self.role, self.entries, self.notify = run, role, entries, notify
        self.index = resume_index(run.path, role, len(entries), since)

    @property
    def current(self) -> AgentSpec:
        return self.entries[self.index]

    def next_for(self, kind: str) -> int | None:
        """The entry to move to after a failure of this kind, or None if none is left."""

        if kind not in SWITCH:
            return None
        here = SUBSCRIPTION.get(self.current.agent, self.current.agent)
        same = (self.current.agent, self.current.model)
        for index in range(self.index + 1, len(self.entries)):
            entry = self.entries[index]
            if kind == "quota":
                if SUBSCRIPTION.get(entry.agent) != here:
                    return index
            elif (entry.agent, entry.model) != same:  # the same model would refuse or fail again
                return index
        return None

    def switch(self, kind: str, message: str, step: str = "") -> bool:
        """Move on after a refusal or a quota; False when the chain has no entry left,
        after telling the user."""

        index = self.next_for(kind)
        before = self.current
        if index is None:
            self.run.event("agent.chain_exhausted", role=self.role, kind=kind, message=message, step=step)
            what = WHAT[kind]
            self.notify(f"{self.role}: every agent {what}", message[:200])
            return False
        self.index = index
        after = self.current
        self.run.event(
            "agent.fallback", role=self.role, index=index, reason=kind, message=message, step=step,
            **{"from": describe(before), "to": describe(after)},
        )
        self.notify(
            f"{self.role} moves to {after.agent} ({after.model})",
            f"{before.agent} ({before.model}) {WHAT[kind]}: {message[:160]}",
        )
        return True


def describe(spec: AgentSpec) -> str:
    return f"{spec.agent} {spec.model} {spec.effort}"


def entries(settings: Config, role: str, primary: AgentSpec | None = None) -> tuple[AgentSpec, ...]:
    """The role's chain: its agent (or a one-off override) then its fallbacks."""

    configured = settings.roles[role]
    return (primary or configured.agent, *configured.fallback)


def resume_index(
    run_dir: Path, role: str, length: int, since: Callable[[dict[str, Any]], bool] | None = None,
) -> int:
    """The entry the role had moved to, from the log; 0 when it never moved."""

    index = 0
    for event in runlog.read_events(run_dir):
        if since is not None and since(event):
            index = 0  # a new history starts here
        elif event.get("event") == "agent.fallback" and event.get("role") == role:
            index = int(event.get("index", 0))
    return index if index < length else 0
