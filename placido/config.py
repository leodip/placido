"""Read a project's .placido/config.toml over built-in defaults and resolve each role."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENTS = ("claude", "codex", "pi")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TIERS = ("fast", "default")
ROLES = ("spec", "implement", "review", "fix", "retro")

DEFAULT_MODELS = {"claude": "opus", "codex": "gpt-6.1-sol", "pi": "deepseek/deepseek-v4.1-flash"}

DEFAULTS: dict[str, Any] = {
    "project": {"base": "main", "setup": "", "teardown": ""},
    "commands": {},
    "gates": {"slice": [], "final": []},
    "roles": {
        "spec": {"agent": "claude", "model": "opus", "effort": "xhigh"},
        "implement": {"agent": "claude", "model": "opus", "effort": "high"},
        "review": {"agent": "codex", "model": "gpt-6.1-sol", "effort": "max", "tier": "fast"},
    },
    "fallback": [
        {"agent": "codex", "model": "gpt-daybreak-blue-latest", "effort": "max"},
        {"agent": "pi", "model": "deepseek/deepseek-v4.1-flash", "effort": "xhigh"},
    ],
    "implement": {"test_first": True, "mutations": 2},
    "review": {"plan_rounds": 0, "final_rounds": 3},
    "limits": {"stage_attempts": 3, "quota_wait": "2h", "ci_wait": "1h"},
}

CONFIG_FILE = Path(".placido") / "config.toml"

COMMAND_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    """One way to run an agent: which one, on which model, how hard it thinks."""

    agent: str
    model: str
    effort: str
    tier: str = "default"

    def args(self) -> list[str]:
        """The agent's own command-line flags for this model and effort."""

        if self.agent == "claude":
            return ["--model", self.model, "--effort", self.effort]
        if self.agent == "codex":
            return [
                "-m", self.model,
                "-c", f"model_reasoning_effort={self.effort}",
                "-c", f"service_tier={self.tier}",
            ]
        return ["--provider", "openrouter", "--model", self.model, "--thinking", self.effort]

    def command(self) -> str:
        return " ".join([self.agent, *self.args()])


@dataclass(frozen=True)
class Command:
    """A project command, offered to agents in their prompts and usable as a gate."""

    name: str
    run: str
    about: str = ""


@dataclass(frozen=True)
class Role:
    name: str
    agent: AgentSpec
    fallback: tuple[AgentSpec, ...]


@dataclass(frozen=True)
class Config:
    source: Path | None
    base: str
    setup: str
    teardown: str
    commands: dict[str, Command]
    slice_gates: tuple[str, ...]  # command names run after each slice, before its commit
    final_gates: tuple[str, ...]  # command names run before the final review
    roles: dict[str, Role]
    test_first: bool
    mutations: int  # killed mutations each slice needs; 0 turns mutation checks off
    plan_rounds: int
    final_rounds: int
    stage_attempts: int
    quota_wait: int  # seconds
    ci_wait: int = 3600  # seconds to wait for the pull request's CI to conclude


def _duration_setting(limits: dict[str, Any], key: str) -> int:
    value = _typed(limits, key, DEFAULTS["limits"][key], "limits")
    try:
        return parse_duration(value)
    except ValueError:
        raise ConfigError(f"limits.{key}: {value!r} is not a duration like 2h or 90m") from None


def override(spec: AgentSpec, agent: str | None, model: str | None, effort: str | None) -> AgentSpec:
    """A role's agent with one-off changes from the command line; changing the agent
    alone also changes the model to that agent's default, as in the config."""

    try:
        if agent is not None:
            _choice(agent, AGENTS, "--agent")
        if effort is not None:
            _choice(effort, EFFORTS, "--effort")
    except ConfigError as error:
        raise ConfigError(str(error).replace(": '", " '", 1)) from None
    new_agent = agent or spec.agent
    if model is None:
        model = spec.model if new_agent == spec.agent else DEFAULT_MODELS[new_agent]
    return AgentSpec(new_agent, model, effort or spec.effort, spec.tier)


def find_config(start: Path) -> Path | None:
    """The nearest .placido/config.toml in start or above it."""

    for folder in (start, *start.parents):
        if (folder / CONFIG_FILE).is_file():
            return folder / CONFIG_FILE
    return None


def parse_duration(text: str) -> int:
    """Seconds in a duration such as 2h, 90m, 45s, or 1h30m."""

    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip())
    if not text.strip() or not match:
        raise ValueError(text)
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def load(path: Path | None) -> Config:
    """Resolve the configuration; with no path, the built-in defaults alone."""

    data: dict[str, Any] = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path}: {error}") from None
    try:
        return _resolve(data, path)
    except ConfigError as error:
        raise ConfigError(f"{path or 'defaults'}: {error}") from None


def _table(data: dict[str, Any], key: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: must be a table")
    _only(value, allowed, key)
    return value


def _only(table: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise ConfigError(f"{where}.{key}: unknown key; expected one of {', '.join(allowed)}")


def _typed(table: dict[str, Any], key: str, default: Any, where: str) -> Any:
    value = table.get(key, default)
    kind = type(default)
    # bool is a subclass of int, so check exactly: `true` is not a round count.
    if type(value) is not kind:
        raise ConfigError(f"{where}.{key}: must be {kind.__name__}, not {value!r}")
    if kind is int and value < 0:
        raise ConfigError(f"{where}.{key}: must not be negative")
    return value


def _choice(value: Any, choices: tuple[str, ...], where: str) -> str:
    if value not in choices:
        raise ConfigError(f"{where}: {value!r} is not one of {', '.join(choices)}")
    return value


def _agent(entry: Any, base: dict[str, Any], where: str, extra: tuple[str, ...] = ()) -> AgentSpec:
    """An agent entry over its defaults; changing the agent alone also changes the model."""

    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: must be a table")
    _only(entry, ("agent", "model", "effort", "tier", *extra), where)
    agent = _choice(entry.get("agent", base.get("agent")), AGENTS, f"{where}.agent")
    model = entry.get("model")
    if model is None:
        model = base["model"] if agent == base.get("agent") else DEFAULT_MODELS[agent]
    if not isinstance(model, str) or not model:
        raise ConfigError(f"{where}.model: must be a model name")
    effort = _choice(entry.get("effort", base.get("effort", "high")), EFFORTS, f"{where}.effort")
    tier = _choice(entry.get("tier", base.get("tier", "default")), TIERS, f"{where}.tier")
    return AgentSpec(agent, model, effort, tier)


def _chain(entries: Any, where: str) -> tuple[AgentSpec, ...]:
    if not isinstance(entries, list):
        raise ConfigError(f"{where}: must be a list of [[{where}]] tables")
    chain = []
    for n, entry in enumerate(entries, 1):
        if isinstance(entry, dict) and "agent" not in entry:
            raise ConfigError(f"{where} #{n}: needs an agent")
        chain.append(_agent(entry, {}, f"{where} #{n}"))
    return tuple(chain)


def _commands(data: dict[str, Any]) -> dict[str, Command]:
    table = data.get("commands", {})
    if not isinstance(table, dict):
        raise ConfigError("commands: must be a table of [commands.<name>] tables")
    commands = {}
    for name, entry in table.items():
        where = f"commands.{name}"
        if not COMMAND_NAME.fullmatch(name):
            raise ConfigError(f"{where}: names use lowercase letters, digits, and dashes")
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: must be a table with run and about")
        _only(entry, ("run", "about"), where)
        run = _typed(entry, "run", "", where)
        if not run.strip():
            raise ConfigError(f"{where}.run: must be a command")
        commands[name] = Command(name, run, _typed(entry, "about", "", where))
    return commands


def _gates(data: dict[str, Any], commands: dict[str, Command]) -> dict[str, tuple[str, ...]]:
    gates = _table(data, "gates", tuple(DEFAULTS["gates"]))
    resolved = {}
    for key in DEFAULTS["gates"]:
        names = gates.get(key, [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"gates.{key}: must be a list of command names")
        for name in names:
            if name not in commands:
                known = ", ".join(commands) or "none defined"
                raise ConfigError(f"gates.{key}: {name!r} is not a command (commands: {known})")
        resolved[key] = tuple(names)
    return resolved


def _resolve(data: dict[str, Any], source: Path | None) -> Config:
    _only(data, tuple(DEFAULTS), "config")
    project = _table(data, "project", tuple(DEFAULTS["project"]))
    lifecycle = {
        key: _typed(project, key, default, "project") for key, default in DEFAULTS["project"].items()
    }
    commands = _commands(data)
    gates = _gates(data, commands)
    fallback = _chain(data.get("fallback", DEFAULTS["fallback"]), "fallback")
    role_tables = _table(data, "roles", ROLES)
    roles = {}
    for name in ROLES:
        entry = role_tables.get(name, {})
        where = f"roles.{name}"
        base, chain = DEFAULTS["roles"].get(name), fallback
        if name == "fix":  # fixing review findings uses the implementer's settings by default
            implementer = roles["implement"]
            base, chain = vars(implementer.agent), implementer.fallback
        if name == "retro":  # a retro is judgment about the project, like the spec interview
            specifier = roles["spec"]
            base, chain = vars(specifier.agent), specifier.fallback
        spec = _agent(entry, base, where, extra=("fallback",))
        if "fallback" in entry:
            chain = _chain(entry["fallback"], f"{where}.fallback")
        roles[name] = Role(name, spec, chain)
    implement = _table(data, "implement", tuple(DEFAULTS["implement"]))
    review = _table(data, "review", tuple(DEFAULTS["review"]))
    limits = _table(data, "limits", tuple(DEFAULTS["limits"]))
    quota_seconds, ci_seconds = (_duration_setting(limits, key) for key in ("quota_wait", "ci_wait"))
    return Config(
        source=source,
        roles=roles,
        test_first=_typed(implement, "test_first", True, "implement"),
        mutations=_typed(implement, "mutations", 2, "implement"),
        plan_rounds=_typed(review, "plan_rounds", 0, "review"),
        final_rounds=_typed(review, "final_rounds", 3, "review"),
        stage_attempts=_typed(limits, "stage_attempts", 3, "limits"),
        quota_wait=quota_seconds,
        ci_wait=ci_seconds,
        commands=commands,
        slice_gates=gates["slice"],
        final_gates=gates["final"],
        **lifecycle,
    )


def command_menu(config: Config) -> str:
    """The project's commands as a prompt section, so agents know what they can run."""

    if not config.commands:
        return ""
    lines = ["## Commands for this project", ""]
    for command in config.commands.values():
        about = f": {command.about}" if command.about else ""
        lines.append(f"- **{command.name}**, `{command.run}`{about}")
    return "\n".join(lines) + "\n"


def format_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "") + (f"{secs}s" if secs else "")
    return text or "0s"


def describe(config: Config, start: Path) -> str:
    """The resolved settings as `placido config` prints them."""

    source = (
        str(config.source)
        if config.source
        else f"built-in defaults (no {CONFIG_FILE} in {start} or above)"
    )
    lines = [f"config  {source}", "", "[project]"]
    for key in ("base", "setup", "teardown"):
        lines.append(f"{key.ljust(8)} {getattr(config, key) or '-'}")
    lines += ["", "[commands]"]
    if config.commands:
        width = max(len(name) for name in config.commands)
        lines.extend(f"{c.name.ljust(width)}  {c.run}" for c in config.commands.values())
    else:
        lines.append("-")
    lines += [
        "",
        "[gates]",
        f"slice  {', '.join(config.slice_gates) or '-'}",
        f"final  {', '.join(config.final_gates) or '-'}",
        "",
        "[roles]",
    ]
    for role in config.roles.values():
        lines.append(f"{role.name.ljust(10)} {role.agent.command()}")
        lines.extend(f"{'  then'.ljust(10)} {spec.command()}" for spec in role.fallback)
    settings = {
        "implement.test_first": str(config.test_first).lower(),
        "implement.mutations": config.mutations,
        "review.plan_rounds": config.plan_rounds,
        "review.final_rounds": config.final_rounds,
        "limits.stage_attempts": config.stage_attempts,
        "limits.quota_wait": format_duration(config.quota_wait),
        "limits.ci_wait": format_duration(config.ci_wait),
    }
    width = max(len(key) for key in settings)
    lines += ["", "[settings]"]
    lines.extend(f"{key.ljust(width)}  {value}" for key, value in settings.items())
    return "\n".join(lines)
