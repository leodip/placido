import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from placido import config, driver, followups, runlog

DRAFT = (
    "# fix: the limiter counts half moves\n\n"
    "Labels: `bug`, `go`\n"
    "Left out because: the limiter is not part of this change.\n"
    "Searched: `gh issue list --state all --search \"limiter\"`: #12, closed.\n\n"
    "The limiter counts each move twice.\n\nDone when: it counts once.\n"
)


def sh(cwd: Path, *argv: str) -> str:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ParseTest(unittest.TestCase):
    def test_header_lines_and_body(self):
        found = followups.parse(DRAFT)
        self.assertEqual(found.title, "fix: the limiter counts half moves")
        self.assertEqual(found.labels, ["bug", "go"])
        self.assertEqual(found.why, "the limiter is not part of this change.")
        self.assertTrue(found.searched.endswith("#12, closed."))
        self.assertEqual(found.body, "The limiter counts each move twice.\n\nDone when: it counts once.")

    def test_an_older_draft_is_all_body(self):
        found = followups.parse("# refactor: replace chi\n\nDrafted earlier.\n\n## Why\n\npprof.\n")
        self.assertEqual((found.title, found.labels, found.why), ("refactor: replace chi", [], ""))
        self.assertEqual(found.body, "Drafted earlier.\n\n## Why\n\npprof.")

    def test_check_names_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "F1.md").write_text(DRAFT)
            (out / "F2.md").write_text("# a title\n\nonly a body\n")
            (out / "F3.md").write_text("Duplicate of followup-x.md\n")
            problems = followups.check_drafts(out, ["F1", "F2", "F3", "F4"])
        self.assertEqual(len(problems), 3)
        self.assertIn("F2.md has no 'Labels:' line", problems[0])
        self.assertIn("F2.md has no 'Left out because:' line", problems[1])
        self.assertTrue(problems[2].endswith("F4.md is missing."))


@dataclass
class Outcome:
    outcome: str
    path: Path


class FakeDrafter:
    """Plays the drafting agent: writes what it is told, then placido's check decides."""

    def __init__(self, write, touch_code=False):
        self.write, self.touch_code, self.calls, self.problems = write, touch_code, [], None

    def __call__(self, role, agent, task, name=None, check=None, **kwargs):
        self.calls.append((role, name, task))
        self.write()
        if self.touch_code:
            (self.worktree / "oops.go").write_text("x")
        self.problems = check(Path("result.md"))
        return Outcome("success" if not self.problems else "invalid", Path("."))


class DraftStepTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "remote", "add", "origin", "https://github.com/leodip/goiabada.git")
        (self.repo / "a.go").write_text("package a\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.run = runlog.Run.create(self.tmp / "runs", "goiabada", "439-x", {"repo": str(self.repo)})
        (self.run.path.parent / "followup-chi.md").write_text("# chi\n\nbody\n")
        self.run.event("review.followups", items=[{"id": "F1", "title": "Limiter", "description": "d", "why": "w"}])

    def draft(self, drafter: FakeDrafter) -> None:
        drafter.worktree = self.repo
        settings = config.load(None)
        ctx = driver.Context(self.run, drafter, None, settings, config.AgentSpec("claude", "opus", "high"), self.repo, {})
        followups.draft(ctx, config.AgentSpec("claude", "opus", "high"))

    def write(self) -> None:
        (self.run.path / "followups" / "F1.md").write_text(DRAFT)
        (self.run.path / "followups" / "notes" / "396.md").write_text("A note.\n")

    def test_drafts_each_follow_up_once(self):
        drafter = FakeDrafter(self.write)
        self.draft(drafter)
        self.draft(drafter)  # a resumed delivery does not draft again
        self.assertEqual(len(drafter.calls), 1)
        role, name, task = drafter.calls[0]
        self.assertEqual((role, name), ("fix", "followups"))
        self.assertIn("**F1** Limiter", task)
        self.assertIn("followup-chi.md", task)  # the interview's drafts, to avoid duplicates
        event = runlog.last_event(self.run.path, "followups.drafted")
        self.assertEqual((event["files"], event["notes"]), (["F1.md"], [396]))
        self.assertEqual(followups.notes(self.run.path), {396: "A note."})

    def test_the_code_must_stay_untouched(self):
        drafter = FakeDrafter(self.write, touch_code=True)
        self.draft(drafter)
        self.assertIn("the worktree changed", drafter.problems[-1])
        self.assertEqual(runlog.last_event(self.run.path, "followups.draft_failed")["outcome"], "invalid")

    def test_a_failed_draft_falls_back_to_what_the_review_said(self):
        self.draft(FakeDrafter(lambda: None))
        drafts = followups.all_drafts(self.run.path)
        self.assertEqual([d.title for d in drafts], ["chi", "Limiter"])
        self.assertEqual((drafts[1].body, drafts[1].why), ("d", "w"))

    def test_no_step_without_follow_ups_or_github(self):
        self.run.event("review.followups", items=[])
        sh(self.repo, "git", "remote", "remove", "origin")
        drafter = FakeDrafter(self.write)
        self.draft(drafter)
        self.assertEqual(drafter.calls, [])


class CommentTest(unittest.TestCase):
    def test_titles_with_quotes_are_shell_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = runlog.Run.create(Path(tmp) / "runs", "p", "1-x", {})
            (run.path.parent / "followup-q.md").write_text("# fix: don't 'quote' $HOME\n\nLabels: `bug`\n\nbody\n")
            text, drafts = followups.comment(run.path, "https://github.com/o/r/pull/1")
        self.assertEqual(len(drafts), 1)
        self.assertIn("gh issue create --title 'fix: don'\"'\"'t '\"'\"'quote'\"'\"' $HOME' --label bug", text)
