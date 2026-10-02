"""Where each issue's run is, in a few words, kept in its run folder and listed
across issues by `placido status`.

Every status starts with one state word, then says where the run is:

    working · slice 2/3         ready · run placido spec
    you · answer in implement-slice-2
    waiting · quota until 15:40
    done · review passed        stopped · slice 2 gave up

The detail stays short; a step is named without its number, as its tab is.
"""

from __future__ import annotations

import re
from pathlib import Path

READY, WORKING, YOU, WAITING, DONE, STOPPED = "ready", "working", "you", "waiting", "done", "stopped"


FILE = "status"  # the latest status, kept in the run folder for `placido status`


def show(run_dir: Path, value: str) -> None:
    """Keep the run's latest status in its run folder."""

    try:
        (run_dir / FILE).write_text(value + "\n", encoding="utf-8")
    except OSError:
        pass


def read(run_dir: Path) -> str:
    try:
        return (run_dir / FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def text(state: str, detail: str = "") -> str:
    return f"{state} · {detail}" if detail else state


def label(step: str) -> str:
    """A step folder's name without its number: `02-implement-slice-1` → `implement-slice-1`."""

    return re.sub(r"^\d+-", "", step)


# ---- every issue at a glance ------------------------------------------------------

# Issues that need the user first, then those still moving, then those at rest.
ORDER = (YOU, STOPPED, WORKING, WAITING, READY, DONE)


def board(runs_root: Path, now: float | None = None) -> str:
    """Every issue placido has not closed, across projects, with its status and how
    long ago anything happened: the glance `placido status` gives."""

    import time

    from placido import runlog

    now = time.time() if now is None else now
    rows = []
    claimed: set[str] = set()
    for run in runlog.all_runs(runs_root):
        events = list(runlog.read_events(run))
        kinds = {e.get("event") for e in events}
        created = next((e for e in reversed(events) if e.get("event") == "worktree.created"), {})
        if "run.closed" in kinds or not created.get("path") or not Path(created["path"]).is_dir():
            continue
        if created["path"] in claimed:
            continue  # a newer run, such as a retried setup, has taken over this worktree
        claimed.add(created["path"])
        value = read(run) or (text(DONE, runlog.outcome(run)) if "run.end" in kinds else text(READY))
        state = value.split(" ", 1)[0]
        if state == DONE:
            value += " · placido close when finished"  # its worktree and stack are still up
        rank = ORDER.index(state) if state in ORDER else len(ORDER)
        last = (run / "events.jsonl").stat().st_mtime if (run / "events.jsonl").is_file() else now
        rows.append((rank, -last, run.parent.parent.name, run.parent.name, value, now - last))
    if not rows:
        return "No issues in progress.\n"
    rows.sort()
    project_width = max(len(r[2]) for r in rows)
    issue_width = max(len(r[3]) for r in rows)
    lines = [
        f"{project.ljust(project_width)}  {issue.ljust(issue_width)}  {value}  ({_ago(age)})"
        for _, _, project, issue, value, age in rows
    ]
    return "\n".join(lines) + "\n"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m ago"
    return f"{seconds // 86400}d ago"
