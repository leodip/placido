"""Delivering a finished run on GitHub: push the branch once, open the pull request
ready for review, post the agreement's release notes and the follow-ups as comments on
it and the notes on other issues the change affects, then wait for CI and fix a red run.

Decided with the user (step 13): nothing is pushed before the review is done, so a
stopped or abandoned run leaves nothing on GitHub; the body closes the run's issue,
and every issue folded in during the interview, with `Closes #N`, so the user's merge
closes them and placido never does. Each action is an event, so a resumed run picks
up where it stopped.

Decided 2026-10-02: the body is written for the person reviewing the change (what it
does, the decisions, what the review found), not the run's log, which stays in
summary.md. Placido files no issues: every follow-up goes, drafted in full with the
command that files it, into one comment on the pull request (placido/followups.py).
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from placido import checks, config, followups, implement, prbody, runlog, spec, start, status, summary
from placido.proc import Runner, run_command

ATTACH_SECONDS = 180  # how long CI may take to show its first check after a push
POLL_SECONDS = 30
PROGRESS_SECONDS = 60  # how often the live view says what CI is still running
LOG_LINES = 400  # the tail of each failed job's log handed to the fixer
JOB = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
COMMENT_ID = re.compile(r"#issuecomment-(\d+)$")
# The review threads of a pull request, each with its comments in order: the first
# started the thread, on a line of the change, and the rest reply to it.
THREADS = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved isOutdated path line originalLine
          comments(first: 100) { nodes { databaseId author { login } body url } }
        }
      }
    }
  }
}"""


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

    def comment(self, kind: str, number: int, body: Path) -> str:
        """Comment on an issue or a pull request (kind "issue" or "pr"); the comment's URL."""

        out = self._call("gh", kind, "comment", str(number), "-R", self.repo, "--body-file", str(body))
        found = re.search(r"https://github\.com/\S+/(?:issues|pull)/\d+#issuecomment-\d+", out)
        return found.group(0) if found else _url(out, "issues" if kind == "issue" else "pull")

    def pr_head(self, number: int) -> str:
        """The commit GitHub has as the pull request's head."""

        return self._call(
            "gh", "pr", "view", str(number), "-R", self.repo, "--json", "headRefOid", "--jq", ".headRefOid",
        ).strip()

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

    def viewer(self) -> str:
        """The login gh acts as: the account placido comments and replies as."""

        return self._call("gh", "api", "user", "--jq", ".login").strip()

    def review_threads(self, number: int) -> list[dict[str, Any]]:
        """The pull request's review threads: whether each is resolved or outdated, where
        it is, and its comments, each with its id, author, body, and URL."""

        owner, name = self.repo.split("/", 1)
        out = self._call(
            "gh", "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
            "-f", f"query={THREADS}",
        )
        try:
            data = json.loads(out)
            nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except (ValueError, KeyError, TypeError):
            raise GitHubError(f"gh api graphql: no review threads in {out.strip()[-300:]}") from None
        threads = []
        for node in nodes or []:
            comments = [
                {"id": c.get("databaseId"), "author": (c.get("author") or {}).get("login") or "",
                 "body": c.get("body") or "", "url": c.get("url") or ""}
                for c in ((node.get("comments") or {}).get("nodes") or [])
            ]
            threads.append({
                "resolved": bool(node.get("isResolved")), "outdated": bool(node.get("isOutdated")),
                "path": node.get("path") or "", "line": node.get("line") or node.get("originalLine"),
                "comments": comments,
            })
        return threads

    def reviews(self, number: int) -> list[dict[str, Any]]:
        """The pull request's reviews, each with its id, author, text, URL, and commit."""

        out = self._call(
            "gh", "api", f"repos/{self.repo}/pulls/{number}/reviews", "--paginate", "--jq",
            ".[] | {id, author: .user.login, body, url: .html_url, commit: .commit_id}",
        )
        try:
            return [json.loads(line) for line in out.splitlines() if line.strip()]
        except ValueError:
            raise GitHubError(f"gh api reviews: {out.strip()[-300:]}") from None

    def reply(self, number: int, comment_id: int, body: Path) -> str:
        """Reply on the review thread a comment is in; the reply's URL."""

        out = self._call(
            "gh", "api", "-X", "POST", f"repos/{self.repo}/pulls/{number}/comments/{comment_id}/replies",
            "-F", f"body=@{body}", "--jq", ".html_url",
        )
        return out.strip()

    def comment_body(self, url: str) -> str | None:
        """The text of the pull request comment at url, as it is now; None once it is gone."""

        found = COMMENT_ID.search(url)
        if not found:
            return None
        result = self._run(
            ["gh", "api", f"repos/{self.repo}/issues/comments/{found.group(1)}", "--jq", ".body"], timeout=120,
        )
        return result.out if result.code == 0 else None

    def edit_comment(self, url: str, body: Path) -> None:
        found = COMMENT_ID.search(url)
        if not found:
            raise GitHubError(f"not a comment's URL: {url}")
        self._call(
            "gh", "api", "-X", "PATCH", f"repos/{self.repo}/issues/comments/{found.group(1)}",
            "-F", f"body=@{body}", "--jq", ".html_url",
        )

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
    """Push, open or update the pull request, file follow-ups, wait for CI and fix it,
    then answer the review comments the project's reviewers left (placido/comments.py).
    Returns "local" without a GitHub remote, else the CI outcome: "green", "red",
    "none" (no checks), or "pending" (still running when ci_wait ran out); or
    "comments-failed" when answering the review comments failed."""

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
    _post_release_notes(github, run, number)
    _post_follow_ups(github, run, record, number)
    github.edit_pr(number, _body(run.path))

    def ci() -> str:
        return await_ci(ctx, github, fixer, number, branch, sleep, clock)

    outcome = ci()
    if outcome in ("green", "none"):
        from placido import comments

        outcome = comments.answer(ctx, github, number, branch, ci, outcome, sleep)
    github.edit_pr(number, _body(run.path))
    return outcome


def await_ci(
    ctx: "driver.Context", github: GitHub, fixer: Any, number: int, branch: str,  # noqa: F821
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> str:
    """Wait for CI on the head pushed last, and fix a red run up to MAX_FIXES times,
    pushing each fix; the outcome, as deliver's. Each wait is numbered in the run, so
    the logs of a later one, after review comments were answered, keep a folder of
    their own."""

    run = ctx.run
    outcome = "none"
    for attempt in range(checks.MAX_FIXES + 1):
        status.show(run.path, status.text(status.WAITING, f"CI on #{number}"))
        head = implement.git(ctx.worktree, "rev-parse", "HEAD")
        run.event("ci.wait", pr=number, commit=head[:12], limit=config.format_duration(ctx.settings.ci_wait))
        outcome, failed = _await_ci(
            github, number, head, ctx.settings.ci_wait, sleep, clock, _progress(run, f"CI on #{number}"),
        )
        run.event("ci.result", outcome=outcome, attempt=attempt + 1, failed=[c.get("name") for c in failed])
        if outcome != "red" or attempt == checks.MAX_FIXES:
            break
        seen = sum(1 for e in runlog.read_events(run.path) if e.get("event") == "ci.result" and e.get("outcome") == "red")
        logs = _save_logs(github, run.path / "ci" / str(seen), failed)
        gates = list(ctx.settings.final_gates or ctx.settings.slice_gates)
        if not checks.fix(ctx, fixer, "the CI checks", logs, gates, f"ci-{seen}"):
            break
        github.push(branch)
        run.event("github.pushed", branch=branch, commit=implement.git(ctx.worktree, "rev-parse", "HEAD"))
    return outcome


def _title(record: dict[str, Any], run_dir: Path) -> str:
    return str(record.get("issue_title") or run_dir.parent.name)


def _await_ci(
    github: GitHub, number: int, head: str, limit: float,
    sleep: Callable[[float], None], clock: Callable[[], float],
    progress: Callable[[str, float], None] = lambda text, seconds: None, arrived: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Wait for the checks on head, the commit just pushed, to conclude: the outcome
    and the failed checks. Until GitHub has moved the pull request to head, its checks
    are the previous commit's: #519 read a fixed failure as red again, one second
    after pushing the fix (2026-10-07). Every PROGRESS_SECONDS it says what it is
    still waiting for, so a long CI run is not mistaken for a hung placido. With
    arrived, the pull request is at head already, so another head means it moved on:
    "moved", at once, where #619's merge waited an hour for a head pushed over
    (2026-10-21)."""

    began = clock()
    moved = clock()
    told = clock()
    while True:
        on_head = github.pr_head(number) == head
        if arrived and not on_head:
            return "moved", []
        if on_head:
            found = github.checks(number)
        else:
            found = []
            moved = clock()
        waited = clock() - began
        if not found and clock() - moved >= ATTACH_SECONDS:
            return "none", []
        if found and not any(c.get("bucket") == "pending" for c in found):
            failed = [c for c in found if c.get("bucket") in ("fail", "cancel")]
            return ("red" if failed else "green"), failed
        if waited >= limit:
            return "pending", []
        if clock() - told >= PROGRESS_SECONDS:
            progress(_waiting_on(found, on_head, head), waited)
            told = clock()
        sleep(POLL_SECONDS)


def _waiting_on(found: list[dict[str, Any]], on_head: bool, head: str) -> str:
    """What the CI wait is still waiting for, in a few words."""

    if not on_head:
        return f"waiting for GitHub to move the pull request to {head[:12]}"
    if not found:
        return "waiting for the checks to start"
    running = [str(c.get("name") or "?") for c in found if c.get("bucket") == "pending"]
    failed = [str(c.get("name") or "?") for c in found if c.get("bucket") in ("fail", "cancel")]
    text = f"{len(found) - len(running)} of {len(found)} checks done; running {_names(running)}"
    return text + (f"; failed so far: {_names(failed)}" if failed else "")


def _names(names: list[str], shown: int = 3) -> str:
    rest = len(names) - shown
    return ", ".join(names[:shown]) + (f", +{rest} more" if rest > 0 else "")


def _progress(run: runlog.Run, label: str) -> Callable[[str, float], None]:
    """A live-view line only, not an event, as the agents' "working" lines (step.py)."""

    def say(text: str, seconds: float) -> None:
        run.progress(f"{label} · {text}", seconds)

    return say


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


def _post_release_notes(github: GitHub, run: runlog.Run, pr_number: int) -> None:
    """The agreement's release notes as a comment on the pull request; once."""

    text = prbody.release_notes(run.path)
    if text and not runlog.last_event(run.path, "release_notes.posted"):
        body = run.path / "release-notes-comment.md"
        body.write_text(text, encoding="utf-8")
        run.event("release_notes.posted", url=github.comment("pr", pr_number, body), on=f"pr {pr_number}")


def _post_follow_ups(github: GitHub, run: runlog.Run, record: dict[str, Any], pr_number: int) -> None:
    """The follow-ups comment on the pull request, then each note on its issue; once each."""

    pr = (runlog.last_event(run.path, "pr.opened") or {}).get("url", "the pull request")
    if not runlog.last_event(run.path, "followups.posted"):
        text, drafts = followups.comment(run.path, pr)
        if text:
            body = run.path / "followups-comment.md"
            body.write_text(text, encoding="utf-8")
            url = github.comment("pr", pr_number, body)
            run.event("followups.posted", url=url, on=f"pr {pr_number}", count=len(drafts))
    noted = {e.get("issue") for e in runlog.read_events(run.path) if e.get("event") == "issue.noted"}
    for number, note in followups.notes(run.path).items():
        if number in noted or number == record.get("issue_number"):
            continue
        body = followups.folder(run.path) / "notes" / f"{number}.posted.md"
        body.write_text(f"{note}\n\nFrom {pr}.\n", encoding="utf-8")
        url = github.comment("issue", number, body)
        run.event("issue.noted", issue=number, url=url)


FOLLOW_UPS = ("followups.posted", "followups.updated", "followups.continued")


def follow_ups_shown(run_dir: Path) -> list[dict[str, Any]]:
    """Each follow-ups comment on the pull request, in order, with how many drafts it
    holds from which number on: the first, as placido last wrote it, then any more."""

    shown: list[dict[str, Any]] = []
    for event in runlog.read_events(run_dir):
        kind = event.get("event")
        if kind == "followups.posted" and not shown:
            shown.append({"url": event.get("url"), "start": 0, "count": int(event.get("count") or 0)})
        elif kind == "followups.updated" and shown:
            shown[0]["count"] = int(event.get("count") or 0)
        elif kind == "followups.continued":
            shown.append({"url": event.get("url"), "start": int(event.get("start") or 0),
                          "count": int(event.get("count") or 0)})
    return shown


def update_follow_ups(github: GitHub, run: runlog.Run, pr_number: int) -> dict[str, tuple[int, str]]:
    """Show every follow-up the run has drafted, after answering the review comments
    drafted more: placido rewrites its follow-ups comment while it reads as placido last
    wrote it, and otherwise posts the new drafts in another comment, numbered on, so an
    edit someone made (an outcome marked on an item) is never lost. Returns, by each
    follow-up's key, its number and the URL of the comment that shows it."""

    pr = (runlog.last_event(run.path, "pr.opened") or {}).get("url", "the pull request")
    drafts = followups.all_drafts(run.path)
    shown = follow_ups_shown(run.path)
    total = sum(entry["count"] for entry in shown)
    if not shown:
        text, drafts = followups.comment(run.path, pr)
        if text:
            body = run.path / "followups-comment.md"
            body.write_text(text, encoding="utf-8")
            run.event("followups.posted", url=github.comment("pr", pr_number, body), on=f"pr {pr_number}",
                      count=len(drafts))
    elif len(drafts) > total:
        first = shown[0]
        written = run.path / "followups-comment.md"
        now = github.comment_body(str(first["url"]))
        same = (len(shown) == 1 and now is not None and written.is_file()
                and _plain(now) == _plain(written.read_text(encoding="utf-8")))
        if same:
            text, drafts = followups.comment(run.path, pr)
            written.write_text(text, encoding="utf-8")
            github.edit_comment(str(first["url"]), written)
            run.event("followups.updated", url=first["url"], count=len(drafts))
        else:
            text, drafts = followups.comment(run.path, pr, start=total, first=str(first["url"]))
            body = run.path / f"followups-comment-{total + 1}.md"
            body.write_text(text, encoding="utf-8")
            url = github.comment("pr", pr_number, body)
            run.event("followups.continued", url=url, start=total, count=len(drafts) - total)
    numbers: dict[str, tuple[int, str]] = {}
    shown = follow_ups_shown(run.path)
    for n, found in enumerate(drafts, 1):
        holder = next((e for e in shown if e["start"] < n <= e["start"] + e["count"]), None)
        if found.key and holder is not None:
            numbers[found.key] = (n, str(holder["url"]))
    return numbers


def _plain(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _body(run_dir: Path) -> Path:
    """The pull request's body, written to the run folder; the run's summary is
    rewritten beside it as the full record."""

    summary.write(run_dir)
    path = run_dir / "pr-body.md"
    path.write_text(prbody.build(run_dir, folded_issues(spec.issue_dir(run_dir))), encoding="utf-8")
    return path


def folded_issues(issue_dir: Path) -> list[int]:
    """GitHub issues the interview folded into the agreement, from its `## Folded in`
    section (skills/spec/SKILL.md), in order: the numbers that lead a bullet, as in
    `- **#5 Its title:**`. A number further on is not one: #641's said "Closes #630",
    a merged pull request, from "- None. #630 built the OTP half…" (2026-10-23)."""

    try:
        text = (issue_dir / "agreement.md").read_text(encoding="utf-8")
    except OSError:
        return []
    found = re.search(r"^#+ Folded in\b.*?$(.*?)(?=^#+ |\Z)", text, re.M | re.S)
    if not found:
        return []
    numbers: list[int] = []
    for line in found.group(1).splitlines():
        item = re.match(r"\s*[-*]\s+(\*\*)?(#\d+.*)", line)
        if not item:
            continue
        lead = item.group(2).split("**", 1)[0] if item.group(1) else item.group(2).split(":", 1)[0]
        for n in re.findall(r"#(\d+)\b", lead):
            if int(n) not in numbers:
                numbers.append(int(n))
    return numbers
