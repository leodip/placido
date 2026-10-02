"""Check that this machine can run placido: herdr, the three agents, logins and billing."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from placido.proc import Result, Runner, run_command

OK, WARN, FAIL = "ok", "warn", "fail"

AGENTS = ("claude", "codex", "pi")

# Any of these switches claude or codex from the subscription to paid API billing.
BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text.strip() else ""


def _version(text: str) -> str:
    """Pull the bare version out of outputs like 'codex-cli 0.160.0' or '2.1.287 (Claude Code)'."""

    match = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return match.group(0) if match else _first_line(text)


def check_installed(name: str, run: Runner, which: Callable[[str], str | None]) -> Check:
    if which(name) is None:
        return Check(name, FAIL, "not found on PATH")
    result = run([name, "--version"])
    if result.code != 0:
        return Check(name, FAIL, f"--version failed: {_first_line(result.out)}")
    return Check(name, OK, _version(result.out))


def tool_versions(
    run: Runner = run_command, which: Callable[[str], str | None] = shutil.which
) -> dict[str, str | None]:
    """Versions of herdr and the agents for run.json; None when a tool is missing or broken."""

    versions: dict[str, str | None] = {}
    for name in ("herdr", *AGENTS):
        check = check_installed(name, run, which)
        versions[name] = check.detail if check.status == OK else None
    return versions


def check_inside_herdr(env: Mapping[str, str]) -> Check:
    if env.get("HERDR_ENV") == "1":
        return Check("herdr pane", OK, f"inside pane {env.get('HERDR_PANE_ID', '?')}")
    return Check("herdr pane", WARN, "not inside a Herdr pane; runs will need one")


def check_claude_login(run: Runner) -> Check:
    name = "claude login"
    result = run(["claude", "auth", "status"])
    try:
        status = json.loads(result.out)
    except ValueError:
        return Check(name, FAIL, f"unreadable auth status: {_first_line(result.out)}")
    if not status.get("loggedIn"):
        return Check(name, FAIL, "not logged in; run `claude` and /login")
    method, provider = status.get("authMethod"), status.get("apiProvider")
    if method != "claude.ai" or provider != "firstParty":
        return Check(name, FAIL, f"logged in with {method} via {provider}, not the subscription")
    return Check(name, OK, "Anthropic subscription")


def check_claude_bypass(home: Path) -> Check:
    """Placido runs claude with approvals bypassed; until its one-time warning is
    accepted, every agent would stop on it. Accepting stores this setting."""

    name = "claude bypass"
    try:
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if settings.get("skipDangerousModePermissionPrompt") is True:
        return Check(name, OK, "bypass-mode warning accepted")
    return Check(
        name, FAIL,
        "run `claude --dangerously-skip-permissions` once and choose \"Yes, I accept\"",
    )


def check_claude_compact(home: Path, env: Mapping[str, str]) -> Check:
    """Long steps rely on claude compacting its context when it fills; with
    auto-compact off, a full context ends the turn in an error instead."""

    name = "claude compact"
    for var in ("DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"):
        if env.get(var, "").strip().lower() not in ("", "0", "false"):
            return Check(name, FAIL, f"unset {var}; placido relies on auto-compact")
    for path in (home / ".claude.json", home / ".claude" / "settings.json"):
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(settings, dict) and settings.get("autoCompactEnabled") is False:
            return Check(name, FAIL, f"auto-compact is off in {path}; turn it on in claude's /config")
    return Check(name, OK, "auto-compact on")


def check_codex_login(run: Runner) -> Check:
    name = "codex login"
    out = run(["codex", "login", "status"]).out
    if "using ChatGPT" in out:
        return Check(name, OK, "ChatGPT subscription")
    if "API key" in out:
        return Check(name, FAIL, "logged in with an API key, not the subscription")
    return Check(name, FAIL, f"not logged in: {_first_line(out)}; run `codex login`")


def check_pi_login(run: Runner) -> Check:
    name = "pi login"
    result = run(["pi", "auth", "check", "--provider", "openrouter", "--json"])
    try:
        status = json.loads(result.out)
    except ValueError:
        return Check(name, FAIL, f"unreadable auth check: {_first_line(result.out)}")
    if status.get("status") != "ready":
        return Check(name, FAIL, f"openrouter is {status.get('status')}; add an OpenRouter key")
    return Check(name, OK, "OpenRouter key")


def check_billing(env: Mapping[str, str]) -> Check:
    found = [var for var in BILLING_VARS if env.get(var)]
    if found:
        return Check("billing", FAIL, f"unset {', '.join(found)}; it can switch agents to paid API billing")
    return Check("billing", OK, "no API key variables set")


def check_integrations(run: Runner) -> list[Check]:
    result = run(["herdr", "integration", "status"])
    states: dict[str, str] = {}
    for line in result.out.splitlines():
        agent, sep, rest = line.partition(":")
        if sep:
            states[agent.strip()] = rest.strip()
    checks = []
    for agent in AGENTS:
        name = f"{agent} integration"
        state = states.get(agent, "")
        if state.startswith("current"):
            checks.append(Check(name, OK, state.split(" (/")[0]))
        elif not state or state.startswith("not installed"):
            checks.append(Check(name, FAIL, f"not installed; run `herdr integration install {agent}`"))
        else:
            checks.append(Check(name, WARN, f"{state.split(' (/')[0]}; run `herdr integration install {agent}`"))
    return checks


LOGIN_CHECKS = {"claude": check_claude_login, "codex": check_codex_login, "pi": check_pi_login}


def run_checks(
    run: Runner = run_command,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Check]:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    herdr = check_installed("herdr", run, which)
    checks = [herdr, check_inside_herdr(env)]
    for agent in AGENTS:
        installed = check_installed(agent, run, which)
        checks.append(installed)
        if installed.status == OK:
            checks.append(LOGIN_CHECKS[agent](run))
            if agent == "claude":
                checks.append(check_claude_bypass(home))
                checks.append(check_claude_compact(home, env))
    checks.append(check_billing(env))
    if herdr.status == OK:
        checks.extend(check_integrations(run))
    return checks


MARKS = {OK: ("✓", "32"), WARN: ("!", "33"), FAIL: ("✗", "31")}


def format_checks(checks: list[Check], color: bool) -> str:
    width = max(len(check.name) for check in checks)
    lines = []
    for check in checks:
        mark, code = MARKS[check.status]
        if color:
            mark = f"\033[{code}m{mark}\033[0m"
        lines.append(f"{mark} {check.name.ljust(width)}  {check.detail}")
    return "\n".join(lines)


def failed(checks: list[Check]) -> bool:
    return any(check.status == FAIL for check in checks)
