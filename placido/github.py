"""Delivering a finished run on GitHub: push the branch once, open the pull request
ready for review with the run's summary as its body, file the follow-ups the user did
not fold in as issues, then wait for CI and fix a red run.

Decided with the user (step 13): nothing is pushed before the review is done, so a
stopped or abandoned run leaves nothing on GitHub; the body closes the run's issue,
and every issue folded in during the interview, with `Closes #N`, so the user's merge
closes them and placido never does; follow-up issues are linked from the body, not a
comment. Each action is an event, so a resumed run picks up where it stopped.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from placido import checks, implement, runlog, spec, start, status, summary
from placido.proc import Runner, run_command

ATTACH_SECONDS = 180  # how long CI may take to show its first check after a push
POLL_SECONDS = 30
LOG_LINES = 400  # the tail of each failed job's log handed to the fixer
JOB = re.compile(r"/actions/runs/(\d+)/job/(\d+)")


class GitHubError(RuntimeError):
    pass


class GitHub:
    """The few gh and git calls delivery needs, against one repository."""

    def __init__(self, repo: str, worktree: Path, run: Runner = run_command) -> None:
        self.repo, self.worktree, self._run = repo, worktree, run

    def _call(self, *argv: str, timeout: float = 120) -> str:
        result = self._run(list(argv), timeout=timeout)
        if result.code != 0:
            raise GitHubError(f"{' '.join(argv[:3])}: {result.out.strip()[-500:]}")
        return result.out

    def push(self, branch: str) -> None:
        self._call("git", "-C", str(self.worktree), "push", "--set-upstream", "origin", branch, timeout=600)

    def open_pr(self, base: str, branch: str, title: str, body: Path) -> str:
        out = self._call(
            "gh", "pr", "create", "-R", self.repo, "--base", base, "--head", branch,
            "--title", title, "--body-file", str(body),
        )
        return _url(out, "pull")

    def edit_pr(self, number: int, body: Path) -> None:
        self._call("gh", "pr", "edit", str(number), "-R", self.repo, "--body-file", str(body))

    def open_issue(self, title: str, body: Path) -> str:
        return _url(self._call("gh", "issue", "create", "-R", self.repo, "--title", title, "--body-file", str(body)), "issues")

    def checks(self, number: int) -> list[dict[str, Any]]:
        """The pull request's checks; empty before CI has attached any."""

        result = self._run(
            ["gh", "pr", "checks", str(number), "-R", self.repo, "--json", "name,bucket,link,workflow"], timeout=120,
        )
        try:
            data = json.loads(result.out or "[]")
        except ValueError:
            if "no checks reported" in result.out.lower():
                return []
            raise GitHubError(f"gh pr checks: {result.out.strip()[-300:]}") from None
        return data if isinstance(data, list) else []

    def failed_log(self, link: str) -> str:
        found = JOB.search(link or "")
        if not found:
            return f"(no log: {link})"
        run_id, job_id = found.groups()
        result = self._run(["gh", "run", "view", run_id, "-R", self.repo, "--job", job_id, "--log-failed"], timeout=300)
        return result.out


def _url(out: str, kind: str) -> str:
    found = re.search(rf"https://github\.com/\S+/{kind}/\d+", out)
    if not found:
        raise GitHubError(f"no {kind} URL in gh's output: {out.strip()[-300:]}")
    return found.group(0)


def number_of(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[1])


# ---- the delivery -------------------------------------------------------------------


def deliver(
    ctx: "driver.Context", fixer: Any, github: GitHub | None = None,  # noqa: F821
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> str:
    """Push, open or update the pull request, file follow-ups, wait for CI and fix it.
    Returns "local" without a GitHub remote, else the CI outcome: "green", "red",
    "none" (no checks), or "pending" (still running when ci_wait ran out)."""

    run = ctx.run
    record = runlog.read_record(run.path)
    root = Path(record.get("repo") or ctx.worktree)
    repo = start.origin_repo(root)
    if repo is None:
        if not runlog.last_event(run.path, "github.skipped"):
            run.event("github.skipped", reason="no GitHub remote; the branch stays local")
        return "local"
    github = github or GitHub(repo, ctx.worktree)
    branch = record.get("branch") or implement.git(ctx.worktree, "rev-parse", "--abbrev-ref", "HEAD")

    status.show(run.path, status.text(status.WORKING, "pushing"))
    github.push(branch)
    run.event("github.pushed", branch=branch, commit=implement.git(ctx.worktree, "rev-parse", "HEAD"))
    opened = runlog.last_event(run.path, "pr.opened")
    if opened is None:
        url = github.open_pr(record.get("base", "main"), branch, _title(record, run.path), _body(run.path))
        run.event("pr.opened", url=url, number=number_of(url))
    number = int((runlog.last_event(run.path, "pr.opened") or {})["number"])
    _file_follow_ups(github, run, record)
    github.edit_pr(number, _body(run.path))

    outcome = "none"
    for attempt in range(checks.MAX_FIXES + 1):
        status.show(run.path, status.text(status.WAITING, f"CI on #{number}"))
        outcome, failed = _await_ci(github, number, ctx.settings.ci_wait, sleep, clock)
        run.event("ci.result", outcome=outcome, attempt=attempt + 1, failed=[c.get("name") for c in failed])
        if outcome != "red" or attempt == checks.MAX_FIXES:
            break
        logs = _save_logs(github, run.path / "ci" / str(attempt + 1), failed)
        gates = list(ctx.settings.final_gates or ctx.settings.slice_gates)
        if not checks.fix(ctx, fixer, "the CI checks", logs, gates, f"ci-{attempt + 1}"):
            break
        github.push(branch)
        run.event("github.pushed", branch=branch, commit=implement.git(ctx.worktree, "rev-parse", "HEAD"))
    github.edit_pr(number, _body(run.path))
    return outcome


def _title(record: dict[str, Any], run_dir: Path) -> str:
    return str(record.get("issue_title") or run_dir.parent.name)


def _await_ci(
    github: GitHub, number: int, limit: float, sleep: Callable[[float], None], clock: Callable[[], float],
) -> tuple[str, list[dict[str, Any]]]:
    """Wait for the checks to conclude: the outcome and the failed checks."""

    began = clock()
    while True:
        found = github.checks(number)
        waited = clock() - began
        if not found and waited >= ATTACH_SECONDS:
            return "none", []
        if found and not any(c.get("bucket") == "pending" for c in found):
            failed = [c for c in found if c.get("bucket") in ("fail", "cancel")]
            return ("red" if failed else "green"), failed
        if waited >= limit:
            return "pending", []
        sleep(POLL_SECONDS)


def _save_logs(github: GitHub, folder: Path, failed: list[dict[str, Any]]) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    logs = []
    for n, check in enumerate(failed, 1):
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{check.get('workflow') or 'ci'}-{check.get('name') or n}").strip("-")
        lines = github.failed_log(str(check.get("link", ""))).splitlines()[-LOG_LINES:]
        path = folder / f"{n:02d}-{name}.log"
        path.write_text(f"# {check.get('name')}: {check.get('link')}\n" + "\n".join(lines) + "\n", encoding="utf-8")
        logs.append(path)
    return logs


def _file_follow_ups(github: GitHub, run: runlog.Run, record: dict[str, Any]) -> None:
    """Open an issue for each follow-up the review left and not yet filed, linking
    back to the pull request. The review logs its follow-ups (`review.followups`), so a
    resumed run files the same ones."""

    filed = {e.get("id") for e in runlog.read_events(run.path) if e.get("event") == "followup.filed"}
    pr = (runlog.last_event(run.path, "pr.opened") or {}).get("url", "")
    source = f"#{record['issue_number']}" if record.get("issue_number") else _title(record, run.path)
    for item in (runlog.last_event(run.path, "review.followups") or {}).get("items", []):
        if item.get("id") in filed:
            continue
        body = (
            f"{item.get('description') or item.get('title')}\n\n"
            f"**Left out of {pr or 'the pull request'} because:** {item.get('why') or 'out of scope'}\n\n"
            f"Found by placido while working on {source}."
        )
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as out:
            out.write(body + "\n")
        url = github.open_issue(str(item.get("title")), Path(out.name))
        Path(out.name).unlink(missing_ok=True)
        run.event("followup.filed", id=item.get("id"), title=item.get("title"), why=item.get("why"), url=url)


def _body(run_dir: Path) -> Path:
    """The pull request's body, written to the run folder: what closes on merge, then
    the run's summary without its title, which is the pull request's own."""

    record = runlog.read_record(run_dir)
    closes = []
    if record.get("issue_number"):
        closes.append(f"Closes #{record['issue_number']}")
    closes += [f"Closes #{n}" for n in folded_issues(spec.issue_dir(run_dir))
               if n != record.get("issue_number")]
    text = summary.write(run_dir)
    # The title is the pull request's own, and the run's state is not the pull
    # request's: the run ends only once CI has answered.
    lines = [line for line in text.splitlines()[1:] if not line.startswith("- **State:**")]
    while lines and not lines[0].strip():
        lines = lines[1:]
    body = "\n".join(closes + [""] + lines).strip() + (
        f"\n\n---\nBuilt by placido · run `{run_dir.parent.name}/{run_dir.name}`\n"
    )
    path = run_dir / "pr-body.md"
    path.write_text(body, encoding="utf-8")
    return path


def folded_issues(issue_dir: Path) -> list[int]:
    """GitHub issues the interview folded into the agreement, from its `## Folded in`
    section (skills/spec/SKILL.md), in order."""

    try:
        text = (issue_dir / "agreement.md").read_text(encoding="utf-8")
    except OSError:
        return []
    found = re.search(r"^#+ Folded in\b.*?$(.*?)(?=^#+ |\Z)", text, re.M | re.S)
    if not found:
        return []
    numbers: list[int] = []
    for n in re.findall(r"#(\d+)\b", found.group(1)):
        if int(n) not in numbers:
            numbers.append(int(n))
    return numbers
