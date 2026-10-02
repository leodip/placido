"""Find the issue a run works on: a GitHub issue through gh, or a local issues/*.md file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from placido.proc import Result, run_command

ISSUES_DIR = "issues"

GitHubRunner = Callable[[list[str]], Result]

_URL = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)\b")
_SHORT = re.compile(r"([\w.-]+)/([\w.-]+)#(\d+)")
_NUMBER = re.compile(r"#?(\d+)")
_REMOTE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_STOPWORDS = {"a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "into"}


class IssueError(RuntimeError):
    pass


@dataclass(frozen=True)
class Issue:
    id: str  # names the branch and the run folder, such as 439-rate-limiter-counting-half
    title: str
    source: str  # the issue's URL, or the local file's path
    text: str  # the full issue as the agents will read it
    number: int | None = None  # the GitHub issue number


def slug(title: str, words: int = 5) -> str:
    """A short name from a title, skipping [bracketed] prefixes and filler words."""

    title = re.sub(r"\[[^\]]*\]", " ", title).lower()
    kept = [word for word in re.findall(r"[a-z0-9]+", title) if word not in _STOPWORDS]
    return "-".join(kept[:words]) or "issue"


def github_repo(remote_url: str) -> str | None:
    """owner/name from a GitHub remote URL, https or ssh; None for any other host."""

    match = _REMOTE.search(remote_url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def resolve(arg: str, root: Path, remote: str | None, gh: GitHubRunner = run_command) -> Issue:
    """The issue named by arg: a GitHub URL, owner/repo#N, or a bare number when the
    repository's origin is on GitHub; otherwise a local issue file by name or prefix."""

    arg = arg.strip()
    match = _URL.match(arg) or _SHORT.fullmatch(arg)
    if match:
        owner, name, number = match.groups()
        wanted = f"{owner}/{name}"
        if remote and wanted.lower() != remote.lower():
            raise IssueError(f"issue is in {wanted}, but this repository's origin is {remote}")
        return fetch_github(wanted, int(number), gh)
    match = _NUMBER.fullmatch(arg)
    if match and remote:
        return fetch_github(remote, int(match.group(1)), gh)
    return read_file(root, arg)


def fetch_github(repo: str, number: int, gh: GitHubRunner = run_command) -> Issue:
    result = gh([
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "number,title,body,labels,state,url,comments",
    ])
    if result.code != 0:
        raise IssueError(f"gh could not read {repo}#{number}: {result.out.strip()}")
    try:
        data = json.loads(result.out)
    except ValueError:
        raise IssueError(f"gh returned unreadable output for {repo}#{number}") from None
    if data.get("state") != "OPEN":
        raise IssueError(f"{repo}#{number} is {str(data.get('state')).lower()}; reopen it to work on it")
    labels = ", ".join(label["name"] for label in data.get("labels", []))
    parts = [f"# {data['title']}", "", f"Issue: {data['url']}"]
    if labels:
        parts.append(f"Labels: {labels}")
    parts += ["", (data.get("body") or "").strip()]
    for comment in data.get("comments", []):
        author = (comment.get("author") or {}).get("login", "?")
        parts += ["", f"## Comment by {author}", "", (comment.get("body") or "").strip()]
    return Issue(
        id=f"{number}-{slug(data['title'])}",
        title=data["title"],
        source=data["url"],
        text="\n".join(parts).rstrip() + "\n",
        number=number,
    )


def read_file(root: Path, arg: str) -> Issue:
    """A local issue file named exactly, or the only one whose name starts with arg."""

    folder = root / ISSUES_DIR
    exact = folder / f"{arg}.md"
    matches = [exact] if exact.is_file() else sorted(folder.glob(f"{arg}*.md"))
    if not matches:
        raise IssueError(f"no issue {arg!r}: not a GitHub issue, and no match in {folder}")
    if len(matches) > 1:
        raise IssueError(f"issue {arg!r} is ambiguous: {', '.join(m.stem for m in matches)}")
    path = matches[0]
    text = path.read_text(encoding="utf-8")
    heading = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
    return Issue(id=path.stem, title=heading, source=str(path), text=text)
