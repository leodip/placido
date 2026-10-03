"""The few Herdr commands placido uses, called through the herdr CLI's JSON output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from placido.proc import Result, run_command


# Runs one herdr command with a timeout in seconds; None waits as long as Herdr does.
HerdrRunner = Callable[[list[str], "float | None"], Result]


class HerdrError(RuntimeError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    workspace_id: str
    pane_id: str


@dataclass(frozen=True)
class AgentInfo:
    status: str
    session: dict[str, Any] | None  # Herdr's agent_session: source, agent, kind, value


class Herdr:
    def __init__(self, run: HerdrRunner = run_command) -> None:
        self.run = run

    def call(self, *args: str, timeout: float | None = 60) -> dict[str, Any]:
        """Run `herdr <args>` and return its result; Herdr reports errors as JSON too."""

        result = self.run(["herdr", *args], timeout)
        if result.code == 0 and not result.out.strip():
            return {}  # some commands, such as report-metadata, print nothing on success
        data = _last_json(result.out)
        if data is None:
            raise HerdrError(f"herdr {args[0]} {args[1]}: {result.out.strip() or f'exit {result.code}'}")
        if "error" in data or result.code != 0:
            error = data.get("error") or {}
            message = error.get("message") or result.out.strip()
            raise HerdrError(f"herdr {args[0]} {args[1]}: {message}", error.get("code", ""))
        return data.get("result", {})

    def create_worktree(self, repo: Path, branch: str, base: str, label: str) -> Worktree:
        # Creating a worktree checks out the whole tree, which takes a while on big repositories.
        result = self.call(
            "worktree", "create", "--cwd", str(repo), "--branch", branch, "--base", base,
            "--label", label, "--no-focus", timeout=300,
        )
        return Worktree(
            path=Path(result["worktree"]["path"]),
            branch=result["worktree"]["branch"],
            workspace_id=result["workspace"]["workspace_id"],
            pane_id=result["root_pane"]["pane_id"],
        )

    def remove_worktree(self, workspace_id: str, force: bool = False) -> None:
        """Remove the workspace's checkout and close the workspace; the branch stays.
        Without force, Herdr refuses a checkout with changes (dirty_worktree_requires_force)."""

        self.call("worktree", "remove", "--workspace", workspace_id, *(["--force"] if force else []),
                  timeout=120)

    def create_tab(
        self, workspace_id: str, cwd: Path, label: str, env: dict[str, str] | None = None
    ) -> str:
        """A new tab in the workspace, its shell given env; returns the tab's pane."""

        pairs = [arg for key, value in (env or {}).items() for arg in ("--env", f"{key}={value}")]
        result = self.call(
            "tab", "create", "--workspace", workspace_id, "--cwd", str(cwd), "--label", label,
            *pairs, "--no-focus",
        )
        return result["root_pane"]["pane_id"]

    def start_agent(self, name: str, kind: str, pane_id: str, args: list[str]) -> None:
        self.call(
            "agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "120000",
            "--", *args, timeout=150,
        )

    def prompt(self, name: str, text: str) -> None:
        self.call("agent", "prompt", name, text)

    def wait(self, name: str, until: tuple[str, ...], timeout: float | None = None) -> str:
        """Wait for one of the given states and return it; None waits indefinitely."""

        args = ["agent", "wait", name]
        for state in until:
            args += ["--until", state]
        if timeout is not None:
            args += ["--timeout", str(int(timeout * 1000))]
        result = self.call(*args, timeout=None if timeout is None else timeout + 30)
        return result.get("agent", {}).get("agent_status", "")

    def agent(self, name: str) -> AgentInfo:
        result = self.call("agent", "get", name)
        agent = result.get("agent", {})
        return AgentInfo(agent.get("agent_status", ""), agent.get("agent_session"))

    def read(self, pane_id: str, source: str = "recent-unwrapped", lines: int = 200) -> str:
        """The pane's screen as plain text, whether or not an agent still runs in it."""

        result = self.run(
            ["herdr", "pane", "read", pane_id, "--source", source, "--lines", str(lines)], 60
        )
        if result.code != 0:
            raise HerdrError(f"herdr pane read: {result.out.strip()}")
        return result.out

    def repo_workspace(self, repo: Path) -> tuple[str, str] | None:
        """The (id, label) of the workspace on the repository's main checkout, under
        which Herdr groups its worktrees; None when there is none."""

        for workspace in self.call("workspace", "list").get("workspaces", []):
            checkout = workspace.get("worktree") or {}
            if checkout.get("checkout_path") == str(repo) and not checkout.get("is_linked_worktree"):
                return str(workspace["workspace_id"]), str(workspace.get("label", ""))
        return None

    def rename_workspace(self, workspace_id: str, label: str) -> None:
        self.call("workspace", "rename", workspace_id, label)

    def focus_workspace(self, workspace_id: str) -> None:
        self.call("workspace", "focus", workspace_id)

    def tab_of(self, pane_id: str) -> str:
        return self.call("pane", "get", pane_id)["pane"]["tab_id"]

    def close_tab(self, tab_id: str) -> None:
        self.call("tab", "close", tab_id)

    def send_keys(self, pane_id: str, *keys: str) -> None:
        self.call("pane", "send-keys", pane_id, *keys)

    def notify(self, title: str, body: str, sound: str = "none") -> None:
        self.call("notification", "show", title, "--body", body, "--sound", sound)


def _last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None
