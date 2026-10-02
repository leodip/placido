"""Follow-ups: the work a run leaves out of its change, drafted as GitHub issues the
user files by hand. Placido files none (decided 2026-10-02).

Two sources: the drafts the interview wrote beside the agreement
(`followup-*.md`), and the review's follow-ups the user did not fold in, which one
agent step drafts after the fold-in question (`skills/followups/SKILL.md`), into the
run's `followups/` folder. Every draft has one format, so placido can build the
command that files it:

    # <issue title>

    Labels: `bug`, `go`
    Left out because: <why this change does not do it>
    Searched: `gh issue list --state all --search "<terms>"`: <what it found>

    <the issue's body: the problem, the evidence, and when it is done>

At delivery all of them go, in full, into one comment on the pull request, each with
its `gh issue create` command.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from placido import decisions, implement, runlog, spec, start

SKILL = Path(__file__).resolve().parent.parent / "skills" / "followups" / "SKILL.md"
META = re.compile(r"^(Labels|Left out because|Searched):\s*(.*)$")
# A review follow-up that an interview draft already covers is drafted as this one
# line, naming that draft, and is not posted twice.
DUPLICATE = "Duplicate of "


@dataclass
class Draft:
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    why: str = ""
    searched: str = ""
    source: Path | None = None  # the draft's file, when there is one


def parse(text: str, source: Path | None = None) -> Draft:
    """A draft from its file. Lenient: an older draft without the meta lines is all body."""

    lines = text.strip().splitlines()
    title = ""
    if lines and lines[0].startswith("# "):
        title, lines = lines[0][2:].strip(), lines[1:]
    meta: dict[str, str] = {}
    rest = list(lines)
    while rest and (not rest[0].strip() or META.match(rest[0])):
        found = META.match(rest.pop(0))
        if found:
            meta[found.group(1)] = found.group(2).strip()
    labels = [part.strip().strip("`").strip() for part in meta.get("Labels", "").split(",")]
    return Draft(
        title=title, body="\n".join(rest).strip(), labels=[label for label in labels if label],
        why=meta.get("Left out because", ""), searched=meta.get("Searched", ""), source=source,
    )


def review_items(run_dir: Path) -> list[dict[str, Any]]:
    """The review's follow-ups the user did not fold in."""

    return list((runlog.last_event(run_dir, "review.followups") or {}).get("items", []))


def folder(run_dir: Path) -> Path:
    return run_dir / "followups"


def all_drafts(run_dir: Path) -> list[Draft]:
    """Every follow-up of the run: the interview's drafts, then the review's, each from
    its drafted file or, when drafting failed, from what the review said."""

    found = [parse(p.read_text(encoding="utf-8"), p) for p in sorted(spec.issue_dir(run_dir).glob("followup-*.md"))]
    for item in review_items(run_dir):
        path = folder(run_dir) / f"{item.get('id')}.md"
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if not text.strip().startswith(DUPLICATE):
                found.append(parse(text, path))
        else:
            found.append(Draft(
                title=str(item.get("title") or item.get("id")),
                body=str(item.get("description") or item.get("title") or ""),
                why=str(item.get("why") or ""),
            ))
    return found


# ---- the drafting step ----------------------------------------------------------------


def draft(ctx: "driver.Context", agent: Any) -> None:  # noqa: F821
    """Have one agent draft the review's follow-ups as issues, once per run. A failed
    step is logged and delivery goes on: the follow-ups are then posted as the review
    wrote them."""

    items = review_items(ctx.run.path)
    if runlog.last_event(ctx.run.path, "followups.drafted") or runlog.last_event(ctx.run.path, "followups.draft_failed"):
        return
    root = Path(runlog.read_record(ctx.run.path).get("repo") or ctx.worktree)
    if not items and start.origin_repo(root) is None:
        return  # nothing to draft, and no GitHub issues to leave notes on
    out = folder(ctx.run.path)
    (out / "notes").mkdir(parents=True, exist_ok=True)
    before = (implement.git(ctx.worktree, "rev-parse", "HEAD"), implement.git(ctx.worktree, "status", "--porcelain"))

    def check(result: Path) -> list[str]:
        problems = check_drafts(out, [str(item.get("id")) for item in items])
        if (implement.git(ctx.worktree, "rev-parse", "HEAD"),
                implement.git(ctx.worktree, "status", "--porcelain")) != before:
            problems.append("the worktree changed; drafting follow-ups must not touch the code. Undo your changes.")
        return problems

    step = ctx.runner("fix", agent, prompt(ctx, items, out), name="followups", check=check)
    if step.outcome != "success":
        ctx.run.event("followups.draft_failed", outcome=step.outcome)
        return
    ctx.run.event("followups.drafted", files=[f"{item.get('id')}.md" for item in items],
                  notes=sorted(notes(ctx.run.path)))


def check_drafts(out: Path, ids: list[str]) -> list[str]:
    problems = []
    for item_id in ids:
        path = out / f"{item_id}.md"
        if not path.is_file():
            problems.append(f"{path} is missing.")
            continue
        text = path.read_text(encoding="utf-8")
        if text.strip().startswith(DUPLICATE):
            continue
        found = parse(text)
        if not found.title:
            problems.append(f"{path.name} must start with '# <issue title>'.")
        if not found.labels:
            problems.append(f"{path.name} has no 'Labels:' line naming at least one of the repository's labels.")
        if not found.why:
            problems.append(f"{path.name} has no 'Left out because:' line.")
        if not found.body:
            problems.append(f"{path.name} has no body after its header lines.")
    return problems


def prompt(ctx: "driver.Context", items: list[dict[str, Any]], out: Path) -> str:  # noqa: F821
    issue = spec.issue_dir(ctx.run.path)
    lines = [
        "# Draft the follow-ups as GitHub issues",
        "",
        f"Follow the placido follow-ups skill in {SKILL}.",
        "",
        f"- **Agreement:** {issue / 'agreement.md'}",
        decisions.prompt_line(ctx.run.path),
        f"- **The review:** {ctx.run.path / 'review.md'}, and each round's folder under {ctx.run.path / 'steps'}.",
        f"- **Code:** the worktree you are in, {ctx.worktree}. Read it; do not change it.",
        f"- **Write:** one draft per follow-up, as {out}/<id>.md.",
    ]
    drafted = sorted(issue.glob("followup-*.md"))
    if drafted:
        lines.append("- **Already drafted by the interview:** " + ", ".join(str(p) for p in drafted))
    lines += [
        "",
        f"- **Notes for other issues:** {out}/notes/<number>.md, for each other issue the change affects.",
        "",
        "## The follow-ups",
        "",
    ]
    if not items:
        lines.append("None: the review left nothing out. Write only the notes, if any issue needs one.")
    for item in items:
        lines.append(f"- **{item.get('id')}** {item.get('title')}")
        if item.get("description"):
            lines.append(f"  - What: {item.get('description')}")
        if item.get("why"):
            lines.append(f"  - Left out because: {item.get('why')}")
    return "\n".join(lines) + "\n"


def notes(run_dir: Path) -> dict[int, str]:
    """The notes the drafting step wrote for other issues the change affects, by number."""

    found = {}
    for path in sorted((folder(run_dir) / "notes").glob("*.md")):
        if path.stem.isdigit() and path.read_text(encoding="utf-8").strip():
            found[int(path.stem)] = path.read_text(encoding="utf-8").strip()
    return found


# ---- the comment ------------------------------------------------------------------------


def comment(run_dir: Path, pr: str) -> tuple[str, list[Draft]]:
    """The pull request comment with every follow-up and the command filing each, and
    the drafts it holds. Each body is also written to a file the command names."""

    drafts = all_drafts(run_dir)
    if not drafts:
        return "", []
    bodies = folder(run_dir) / "bodies"
    bodies.mkdir(parents=True, exist_ok=True)
    parts = [
        "## Follow-ups",
        "",
        f"{len(drafts)} piece{'s' if len(drafts) != 1 else ''} of work left out of {pr}, each drafted as an"
        " issue. Nothing is filed: open a draft, and run its command to file it.",
    ]
    for n, found in enumerate(drafts, 1):
        body_file = bodies / f"{n}.md"
        body_file.write_text(found.body + "\n", encoding="utf-8")
        command = "gh issue create --title " + shlex.quote(found.title) + "".join(
            f" --label {shlex.quote(label)}" for label in found.labels
        ) + f" --body-file {shlex.quote(str(body_file))}"
        parts += ["", f"### {n}. {found.title}", ""]
        if found.why:
            parts += [f"Left out because: {found.why}", ""]
        parts += ["<details><summary>The draft, and the command to file it</summary>", ""]
        meta = []
        if found.labels:
            meta.append("Labels: " + ", ".join(f"`{label}`" for label in found.labels))
        if found.searched:
            meta.append(f"Searched: {found.searched}")
        if meta:
            parts += [" · ".join(meta), ""]
        parts += ["> " + line if line.strip() else ">" for line in found.body.splitlines()]
        parts += ["", "```bash", command, "```", "", "</details>"]
    return "\n".join(parts) + "\n", drafts
