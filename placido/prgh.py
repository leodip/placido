"""What `placido pr` reads from GitHub and does there, through gh.

Reading: the pull request, the issues it closes, the review placido posted on it in an
earlier round, and the CI runs GitHub holds for approval. Doing, each only once the
user has chosen it at the review's menu: posting a review or a comment, approving CI
runs, merging, and closing. Pushing to the contributor's branch is git's, in
placido/prmenu.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from placido.proc import Runner, run_command

# Opens the hidden line placido ends everything it posts with, so a later round finds
# the review it posted: `<!-- placido pr review · round 1 · 0123456789ab -->`. Only the
# user's own comments count, since anyone can write the same line in theirs.
MARKER = "<!-- placido pr review"

FIELDS = (
    "number,title,body,url,state,isDraft,author,baseRefName,headRefName,headRefOid,"
    "headRepository,headRepositoryOwner,isCrossRepository,maintainerCanModify,additions,"
    "deletions,changedFiles,closingIssuesReferences"
)


class PRError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    url: str
    state: str  # OPEN, CLOSED, or MERGED
    draft: bool
    author: str
    base: str  # the branch it merges into
    head_ref: str  # its branch, in head_repo
    head_sha: str
    head_repo: str  # owner/name of the repository its branch lives in
    cross: bool  # from a fork
    can_modify: bool  # the maintainers may push to its branch
    additions: int
    deletions: int
    files: int
    closes: tuple[int, ...]  # issues it closes when merged


def marker(round_: int, head: str) -> str:
    return f"{MARKER} · round {round_} · {head[:12]} -->"


class Hub:
    """The gh calls `placido pr` makes against one repository."""

    def __init__(self, repo: str, run: Runner = run_command) -> None:
        self.repo, self._run = repo, run

    def _call(self, *argv: str, timeout: float = 120) -> str:
        result = self._run(list(argv), timeout=timeout)
        if result.code != 0:
            raise PRError(f"{' '.join(argv[:3])}: {result.out.strip()[-500:]}")
        return result.out

    def _json(self, *argv: str) -> Any:
        out = self._call(*argv)
        try:
            return json.loads(out)
        except ValueError:
            raise PRError(f"{' '.join(argv[:3])}: not JSON: {out.strip()[-300:]}") from None

    def login(self) -> str:
        return self._call("gh", "api", "user", "--jq", ".login").strip()

    def pull_request(self, number: int) -> PullRequest:
        data = self._json("gh", "pr", "view", str(number), "-R", self.repo, "--json", FIELDS)
        head = data.get("headRepository") or {}
        owner = (data.get("headRepositoryOwner") or {}).get("login", "")
        return PullRequest(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            url=str(data.get("url") or ""),
            state=str(data.get("state") or ""),
            draft=bool(data.get("isDraft")),
            author=str((data.get("author") or {}).get("login", "")),
            base=str(data.get("baseRefName") or ""),
            head_ref=str(data.get("headRefName") or ""),
            head_sha=str(data.get("headRefOid") or ""),
            head_repo=f"{owner}/{head.get('name', '')}" if owner else self.repo,
            cross=bool(data.get("isCrossRepository")),
            can_modify=bool(data.get("maintainerCanModify")),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            files=int(data.get("changedFiles") or 0),
            closes=tuple(
                int(i["number"]) for i in data.get("closingIssuesReferences") or [] if i.get("number")
            ),
        )

    def issue(self, number: int) -> tuple[str, str]:
        """An issue's title and body."""

        data = self._json("gh", "issue", "view", str(number), "-R", self.repo, "--json", "title,body")
        return str(data.get("title") or ""), str(data.get("body") or "")

    def earlier_review(self, number: int, login: str) -> str | None:
        """The latest review or comment the user posted through placido on the pull
        request, or None."""

        data = self._json("gh", "pr", "view", str(number), "-R", self.repo, "--json", "comments,reviews")
        posted = []
        for item in [*(data.get("comments") or []), *(data.get("reviews") or [])]:
            body = str(item.get("body") or "")
            if MARKER in body and (item.get("author") or {}).get("login") == login:
                posted.append((str(item.get("createdAt") or item.get("submittedAt") or ""), body))
        return sorted(posted)[-1][1] if posted else None

    def waiting_runs(self, head: str) -> list[dict[str, Any]]:
        """The workflow runs on head that wait for a maintainer's approval, as GitHub
        holds a first-time contributor's."""

        runs = self._json(
            "gh", "run", "list", "-R", self.repo, "--commit", head, "--limit", "50",
            "--json", "databaseId,name,workflowName,status,conclusion,event",
        )
        return [r for r in runs if isinstance(r, dict) and r.get("conclusion") == "action_required"]

    def approve_run(self, run_id: int) -> None:
        self._call("gh", "api", "-X", "POST", f"repos/{self.repo}/actions/runs/{run_id}/approve")

    def review(self, number: int, event: str, body: Path) -> None:
        """Post a review: event is comment or approve. Placido asks no contributor for
        changes: the maintainer makes them (decided 2026-10-17)."""

        if event not in ("comment", "approve"):
            raise ValueError(event)
        self._call("gh", "pr", "review", str(number), "-R", self.repo, f"--{event}", "--body-file", str(body))

    def comment(self, number: int, body: Path) -> str:
        out = self._call("gh", "pr", "comment", str(number), "-R", self.repo, "--body-file", str(body))
        found = re.search(r"https://github\.com/\S+#issuecomment-\d+", out)
        return found.group(0) if found else ""

    def merge(self, number: int, head: str, subject: str, body: Path) -> None:
        """Squash-merge, only if the pull request's head is still the commit reviewed."""

        self._call(
            "gh", "pr", "merge", str(number), "-R", self.repo, "--squash", "--match-head-commit", head,
            "--subject", subject, "--body-file", str(body), timeout=300,
        )

    def close(self, number: int) -> None:
        self._call("gh", "pr", "close", str(number), "-R", self.repo)

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
            raise PRError(f"gh pr checks: {result.out.strip()[-300:]}") from None
        return data if isinstance(data, list) else []

    def pr_head(self, number: int) -> str:
        return self._call(
            "gh", "pr", "view", str(number), "-R", self.repo, "--json", "headRefOid", "--jq", ".headRefOid",
        ).strip()


def ci_state(checks: list[dict[str, Any]]) -> str:
    """green, red, pending, or none, from a pull request's checks."""

    if not checks:
        return "none"
    if any(c.get("bucket") == "pending" for c in checks):
        return "pending"
    if any(c.get("bucket") in ("fail", "cancel") for c in checks):
        return "red"
    return "green"
