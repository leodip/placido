import json
import tempfile
import unittest
from pathlib import Path

from placido import issues
from placido.issues import IssueError
from placido.proc import Result

ISSUE_439 = {
    "number": 439,
    "title": "[Package review 16/20] Rate limiter: the counting half moves into ratelimit",
    "body": "## Summary\n\nMove the counting half.\n",
    "labels": [{"name": "enhancement"}, {"name": "go"}],
    "state": "OPEN",
    "url": "https://github.com/leodip/goiabada/issues/439",
    "comments": [{"author": {"login": "leodip"}, "body": "Also rename the tiers."}],
}


class FakeGh:
    def __init__(self, data: dict | None = None, result: Result | None = None) -> None:
        self.result = result or Result(0, json.dumps(data or ISSUE_439))
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> Result:
        self.calls.append(argv)
        return self.result


class SlugTest(unittest.TestCase):
    def test_skips_brackets_and_filler(self):
        self.assertEqual(
            issues.slug("[Package review 16/20] Rate limiter: the counting half moves into ratelimit"),
            "rate-limiter-counting-half-moves",
        )

    def test_short_and_empty_titles(self):
        self.assertEqual(issues.slug("Fix it"), "fix-it")
        self.assertEqual(issues.slug("[WIP]"), "issue")


class GithubRepoTest(unittest.TestCase):
    def test_remote_forms(self):
        for url in (
            "https://github.com/leodip/goiabada.git",
            "https://github.com/leodip/goiabada",
            "git@github.com:leodip/goiabada.git",
            "ssh://git@github.com/leodip/goiabada.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(issues.github_repo(url), "leodip/goiabada")

    def test_other_hosts(self):
        self.assertIsNone(issues.github_repo("https://gitlab.com/leodip/goiabada.git"))


class ResolveGithubTest(unittest.TestCase):
    root = Path("/nowhere")

    def test_every_form_reads_the_same_issue(self):
        for arg in (
            "https://github.com/leodip/goiabada/issues/439",
            "https://github.com/leodip/goiabada/issues/439#issuecomment-1",
            "leodip/goiabada#439",
            "439",
            "#439",
        ):
            with self.subTest(arg=arg):
                gh = FakeGh()
                issue = issues.resolve(arg, self.root, "leodip/goiabada", gh)
                self.assertEqual(issue.id, "439-rate-limiter-counting-half-moves")
                self.assertEqual(gh.calls[0][:6], ["gh", "issue", "view", "439", "--repo", "leodip/goiabada"])

    def test_url_works_without_a_github_origin(self):
        issue = issues.resolve("https://github.com/leodip/goiabada/issues/439", self.root, None, FakeGh())
        self.assertEqual(issue.source, "https://github.com/leodip/goiabada/issues/439")

    def test_issue_from_another_repository_is_refused(self):
        with self.assertRaisesRegex(IssueError, "issue is in leodip/ai, but this repository's origin"):
            issues.resolve("leodip/ai#1", self.root, "leodip/goiabada", FakeGh())

    def test_repository_names_compare_without_case(self):
        issue = issues.resolve("LeoDip/Goiabada#439", self.root, "leodip/goiabada", FakeGh())
        self.assertEqual(issue.title, ISSUE_439["title"])

    def test_text_has_title_link_labels_body_and_comments(self):
        text = issues.resolve("439", self.root, "leodip/goiabada", FakeGh()).text
        self.assertEqual(
            text,
            "# [Package review 16/20] Rate limiter: the counting half moves into ratelimit\n"
            "\n"
            "Issue: https://github.com/leodip/goiabada/issues/439\n"
            "Labels: enhancement, go\n"
            "\n"
            "## Summary\n\nMove the counting half.\n"
            "\n"
            "## Comment by leodip\n\nAlso rename the tiers.\n",
        )

    def test_closed_issue_is_refused(self):
        with self.assertRaisesRegex(IssueError, "leodip/goiabada#439 is closed"):
            issues.resolve("439", self.root, "leodip/goiabada", FakeGh({**ISSUE_439, "state": "CLOSED"}))

    def test_gh_failure(self):
        gh = FakeGh(result=Result(1, "GraphQL: Could not resolve to an issue"))
        with self.assertRaisesRegex(IssueError, "gh could not read leodip/goiabada#9: GraphQL"):
            issues.resolve("9", self.root, "leodip/goiabada", gh)


class ResolveFileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "issues").mkdir()
        (self.root / "issues" / "01-shout-flag.md").write_text("# Add a --shout option\n\nBody.\n")
        (self.root / "issues" / "02-many-names.md").write_text("No heading.\n")

    def test_a_number_without_a_github_origin_is_a_file_prefix(self):
        gh = FakeGh()
        issue = issues.resolve("01", self.root, None, gh)
        self.assertEqual(issue.id, "01-shout-flag")
        self.assertEqual(issue.title, "Add a --shout option")
        self.assertEqual(issue.text, "# Add a --shout option\n\nBody.\n")
        self.assertEqual(gh.calls, [])

    def test_full_name(self):
        self.assertEqual(issues.resolve("02-many-names", self.root, None).title, "02-many-names")

    def test_names_are_files_even_with_a_github_origin(self):
        issue = issues.resolve("01-shout-flag", self.root, "leodip/goiabada", FakeGh())
        self.assertEqual(issue.source, str(self.root / "issues" / "01-shout-flag.md"))

    def test_missing_and_ambiguous(self):
        with self.assertRaisesRegex(IssueError, "no issue 'x'"):
            issues.resolve("x", self.root, None)
        with self.assertRaisesRegex(IssueError, "ambiguous: 01-shout-flag, 02-many-names"):
            issues.resolve("0", self.root, None)


if __name__ == "__main__":
    unittest.main()
