"""Run folders: run.json, the append-only events.jsonl, and one folder per step."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO

Clock = Callable[[], datetime]


def local_now() -> datetime:
    return datetime.now().astimezone()


def runs_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("PLACIDO_RUNS_DIR") or Path.home() / "placido" / "runs")


def new_run_id(now: datetime) -> str:
    """Sortable and safe in paths, like 2026-10-02T153012."""

    return now.strftime("%Y-%m-%dT%H%M%S")


class Run:
    """One run of placido on one issue; the only writer of its events.jsonl."""

    def __init__(self, path: Path, clock: Clock = local_now, echo: TextIO | None = None) -> None:
        self.path = path
        self.clock = clock
        self.echo = echo

    @classmethod
    def create(
        cls,
        root: Path,
        project: str,
        issue: str,
        info: Mapping[str, Any],
        clock: Clock = local_now,
        echo: TextIO | None = None,
    ) -> "Run":
        started = clock()
        issue_dir = root / project / issue
        issue_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(started)
        path, n = issue_dir / run_id, 1
        while path.exists():
            n += 1
            path = issue_dir / f"{run_id}-{n}"
        path.mkdir()
        record = {
            "run_id": path.name,
            "project": project,
            "issue": issue,
            "started": started.isoformat(timespec="seconds"),
            **info,
        }
        (path / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return cls(path, clock, echo)

    @property
    def events_path(self) -> Path:
        return self.path / "events.jsonl"

    def event(self, kind: str, /, **fields: Any) -> dict[str, Any]:
        """Append one event; the file is closed after every line so a crash loses nothing."""

        record = {"ts": self.clock().isoformat(timespec="milliseconds"), "event": kind, **fields}
        with self.events_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.echo is not None:
            print(format_event(record), file=self.echo, flush=True)
        return record

    def step_dir(self, number: int, name: str) -> Path:
        path = self.path / "steps" / f"{number:02d}-{name}"
        path.mkdir(parents=True, exist_ok=True)
        return path


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the events of a run folder; a torn last line is skipped, not fatal."""

    try:
        lines = (path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def format_event(event: Mapping[str, Any]) -> str:
    """One readable line: time, event name, then its fields as key=value."""

    ts = str(event.get("ts", ""))
    clock = ts[11:19] if len(ts) >= 19 else ts
    fields = " ".join(
        f"{key}={_value(value)}" for key, value in event.items() if key not in ("ts", "event")
    )
    return f"{clock}  {str(event.get('event', '?')).ljust(12)}  {fields}".rstrip()


def _value(value: Any) -> str:
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False) if not value or any(c.isspace() for c in value) else value


def all_runs(root: Path) -> list[Path]:
    """Every run under the runs folder, across projects and issues, newest first."""

    runs = [run_json.parent for run_json in root.glob("*/*/*/run.json")]
    return sorted(runs, key=lambda run: (run / "run.json").stat().st_mtime, reverse=True)


def latest_run(root: Path) -> Path | None:
    runs = all_runs(root)
    return runs[0] if runs else None


def read_record(path: Path) -> dict[str, Any]:
    """A run's run.json."""

    return json.loads((path / "run.json").read_text(encoding="utf-8"))


def active_runs(root: Path, project: str, repo: str | None = None) -> list[Path]:
    """The project's runs that are ready for steps and not yet closed, newest first.
    With repo, only runs of that repository, so two projects sharing a folder name
    never mix.

    A run whose worktree is gone, or was taken over by a newer run of the same issue,
    is not active either: its worktree was removed outside placido, so nothing more
    can happen in it."""

    found = []
    claimed: set[str] = set()
    for run in all_runs(root):
        if run.parent.parent.name != project:
            continue
        if repo is not None:
            try:
                recorded = read_record(run).get("repo")
            except (OSError, ValueError):
                continue
            if recorded not in (None, repo):
                continue
        events = list(read_events(run))
        kinds = {event.get("event") for event in events}
        if "run.ready" not in kinds or "run.closed" in kinds:
            continue  # a run stays selectable after it ends, until `placido close`
        worktree = next(
            (e.get("path") for e in reversed(events) if e.get("event") == "worktree.created"), None
        )
        if not worktree or worktree in claimed or not Path(worktree).is_dir():
            continue
        claimed.add(worktree)
        found.append(run)
    return found


def active_run(root: Path, project: str) -> Path | None:
    """The project's newest active run."""

    runs = active_runs(root, project)
    return runs[0] if runs else None


def last_event(path: Path, kind: str) -> dict[str, Any] | None:
    found = None
    for event in read_events(path):
        if event.get("event") == kind:
            found = event
    return found


def outcome(path: Path) -> str:
    """The outcome of the run's run.end event, or "unfinished" without one."""

    ends = [e for e in read_events(path) if e.get("event") == "run.end"]
    return str(ends[-1].get("outcome", "?")) if ends else "unfinished"


def resolve_run(arg: str, root: Path) -> Path:
    """A run folder given as a path, or relative to the runs folder as `runs` prints it."""

    path = Path(arg)
    return path if path.exists() or path.is_absolute() else root / path
