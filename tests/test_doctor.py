import json
import tempfile
import unittest
from pathlib import Path

from placido import doctor
from placido.doctor import FAIL, OK, WARN, Result

CLAUDE_SUBSCRIPTION = json.dumps(
    {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
)
INTEGRATIONS = """pi: current (v9) (/home/u/.pi/agent/extensions/herdr-agent-state.ts)
omp: not installed (/home/u/.omp/agent/extensions/herdr-omp-agent-state.ts)
claude: current (v10) (/home/u/.claude/hooks/herdr-agent-state.sh)
codex: current (v8) (/home/u/.codex/herdr-agent-state.sh)"""

HEALTHY = {
    "herdr --version": Result(0, "herdr 0.9.3"),
    "claude --version": Result(0, "2.1.287 (Claude Code)"),
    "codex --version": Result(0, "codex-cli 0.160.0"),
    "pi --version": Result(0, "1.0.0"),
    "claude auth status": Result(0, CLAUDE_SUBSCRIPTION),
    "codex login status": Result(0, "Logged in using ChatGPT"),
    "pi auth check --provider openrouter --json": Result(
        0, '{"status":"ready","provider":"openrouter","authType":"api_key"}'
    ),
    "herdr integration status": Result(0, INTEGRATIONS),
}


class FakeMachine:
    """Answers commands from a table, so each test changes only what it is about."""

    def __init__(self, answers: dict[str, Result] | None = None) -> None:
        self.answers = {**HEALTHY, **(answers or {})}
        self.missing: set[str] = set()
        self.env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "p1"}
        tmp = tempfile.TemporaryDirectory()
        self._tmp = tmp  # removed when the machine is garbage collected
        self.home = Path(tmp.name)
        self.claude_settings({"skipDangerousModePermissionPrompt": True})

    def claude_settings(self, settings: dict | None) -> None:
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(exist_ok=True)
        if settings is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(settings))

    def run(self, argv: list[str]) -> Result:
        return self.answers[" ".join(argv)]

    def which(self, name: str) -> str | None:
        return None if name in self.missing else f"/usr/bin/{name}"

    def checks(self) -> dict[str, doctor.Check]:
        found = doctor.run_checks(run=self.run, which=self.which, env=self.env, home=self.home)
        return {check.name: check for check in found}


class HealthyMachineTest(unittest.TestCase):
    def test_everything_is_green(self):
        checks = FakeMachine().checks()
        self.assertEqual(
            list(checks),
            [
                "herdr", "herdr pane",
                "claude", "claude login", "claude bypass", "claude compact",
                "codex", "codex login",
                "pi", "pi login",
                "billing",
                "claude integration", "codex integration", "pi integration", "email",
            ],
        )
        self.assertEqual({c.status for c in checks.values()}, {OK})
        self.assertEqual(checks["herdr"].detail, "0.9.3")
        self.assertEqual(checks["claude"].detail, "2.1.287")
        self.assertEqual(checks["codex"].detail, "0.160.0")
        self.assertEqual(checks["pi"].detail, "1.0.0")
        self.assertEqual(checks["claude integration"].detail, "current (v10)")


class HerdrTest(unittest.TestCase):
    def test_missing_herdr_fails_and_skips_integrations(self):
        machine = FakeMachine()
        machine.missing.add("herdr")
        checks = machine.checks()
        self.assertEqual(checks["herdr"].status, FAIL)
        self.assertNotIn("claude integration", checks)

    def test_outside_a_pane_is_only_a_warning(self):
        machine = FakeMachine()
        machine.env = {}
        checks = machine.checks()
        self.assertEqual(checks["herdr pane"].status, WARN)
        self.assertFalse(doctor.failed(list(checks.values())))


class AgentTest(unittest.TestCase):
    def test_missing_agent_fails_and_skips_its_login(self):
        machine = FakeMachine()
        machine.missing.add("codex")
        checks = machine.checks()
        self.assertEqual(checks["codex"].status, FAIL)
        self.assertNotIn("codex login", checks)

    def test_unrecognized_version_shows_the_raw_line(self):
        checks = FakeMachine({"pi --version": Result(0, "dev build")}).checks()
        self.assertEqual(checks["pi"].detail, "dev build")

    def test_broken_version_command_fails(self):
        checks = FakeMachine({"pi --version": Result(1, "boom")}).checks()
        self.assertEqual(checks["pi"].status, FAIL)


class LoginTest(unittest.TestCase):
    def claude(self, status: dict) -> doctor.Check:
        return FakeMachine({"claude auth status": Result(0, json.dumps(status))}).checks()["claude login"]

    def test_claude_logged_out_fails(self):
        self.assertEqual(self.claude({"loggedIn": False}).status, FAIL)

    def test_claude_on_an_api_key_fails(self):
        check = self.claude({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})
        self.assertEqual(check.status, FAIL)
        self.assertIn("not the subscription", check.detail)

    def test_claude_through_a_cloud_provider_fails(self):
        check = self.claude({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"})
        self.assertEqual(check.status, FAIL)

    def test_claude_unreadable_status_fails(self):
        check = FakeMachine({"claude auth status": Result(1, "error: boom")}).checks()["claude login"]
        self.assertEqual(check.status, FAIL)

    def test_codex_on_an_api_key_fails(self):
        out = Result(0, "Logged in using an API key - sk-proj-***")
        check = FakeMachine({"codex login status": out}).checks()["codex login"]
        self.assertEqual(check.status, FAIL)
        self.assertIn("API key", check.detail)

    def test_codex_logged_out_fails(self):
        check = FakeMachine({"codex login status": Result(1, "Not logged in")}).checks()["codex login"]
        self.assertEqual(check.status, FAIL)

    def test_pi_without_openrouter_key_fails(self):
        out = Result(1, '{"status":"missing","provider":"openrouter"}')
        machine = FakeMachine({"pi auth check --provider openrouter --json": out})
        self.assertEqual(machine.checks()["pi login"].status, FAIL)


class BypassTest(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(FakeMachine().checks()["claude bypass"].status, OK)

    def test_not_accepted_or_no_settings(self):
        for settings in ({"theme": "dark"}, {"skipDangerousModePermissionPrompt": False}, None):
            with self.subTest(settings=settings):
                machine = FakeMachine()
                machine.claude_settings(settings)
                check = machine.checks()["claude bypass"]
                self.assertEqual(check.status, FAIL)
                self.assertIn("--dangerously-skip-permissions", check.detail)

    def test_unreadable_settings(self):
        machine = FakeMachine()
        (machine.home / ".claude" / "settings.json").write_text("{not json")
        self.assertEqual(machine.checks()["claude bypass"].status, FAIL)


class CompactTest(unittest.TestCase):
    def test_on_by_default(self):
        self.assertEqual(FakeMachine().checks()["claude compact"].status, OK)

    def test_turned_off_in_claude_config(self):
        machine = FakeMachine()
        (machine.home / ".claude.json").write_text(json.dumps({"autoCompactEnabled": False}))
        check = machine.checks()["claude compact"]
        self.assertEqual(check.status, FAIL)
        self.assertIn(".claude.json", check.detail)

    def test_turned_off_in_settings(self):
        machine = FakeMachine()
        machine.claude_settings({"skipDangerousModePermissionPrompt": True, "autoCompactEnabled": False})
        self.assertEqual(machine.checks()["claude compact"].status, FAIL)

    def test_turned_off_by_a_variable(self):
        for var in ("DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"):
            with self.subTest(var=var):
                machine = FakeMachine()
                machine.env[var] = "1"
                check = machine.checks()["claude compact"]
                self.assertEqual(check.status, FAIL)
                self.assertIn(var, check.detail)

    def test_a_variable_set_to_false_is_fine(self):
        machine = FakeMachine()
        machine.env["DISABLE_AUTO_COMPACT"] = "0"
        self.assertEqual(machine.checks()["claude compact"].status, OK)


class BillingTest(unittest.TestCase):
    def test_each_api_key_variable_fails(self):
        for var in doctor.BILLING_VARS:
            with self.subTest(var=var):
                machine = FakeMachine()
                machine.env[var] = "sk-123"
                check = machine.checks()["billing"]
                self.assertEqual(check.status, FAIL)
                self.assertIn(var, check.detail)

    def test_empty_variable_is_ignored(self):
        machine = FakeMachine()
        machine.env["OPENAI_API_KEY"] = ""
        self.assertEqual(machine.checks()["billing"].status, OK)


class IntegrationTest(unittest.TestCase):
    def status(self, text: str) -> dict[str, doctor.Check]:
        return FakeMachine({"herdr integration status": Result(0, text)}).checks()

    def test_not_installed_fails(self):
        checks = self.status(INTEGRATIONS.replace("codex: current (v8)", "codex: not installed"))
        self.assertEqual(checks["codex integration"].status, FAIL)
        self.assertIn("herdr integration install codex", checks["codex integration"].detail)

    def test_absent_from_the_list_fails(self):
        checks = self.status("claude: current (v10) (/x)\ncodex: current (v8) (/y)")
        self.assertEqual(checks["pi integration"].status, FAIL)

    def test_outdated_warns(self):
        checks = self.status(INTEGRATIONS.replace("pi: current (v9)", "pi: outdated (v8, current v9)"))
        self.assertEqual(checks["pi integration"].status, WARN)


class FormatTest(unittest.TestCase):
    def test_aligned_lines_without_color(self):
        text = doctor.format_checks(
            [doctor.Check("herdr", OK, "herdr 0.9.3"), doctor.Check("billing", FAIL, "unset X")],
            color=False,
        )
        self.assertEqual(text, "✓ herdr    herdr 0.9.3\n✗ billing  unset X")

    def test_color_wraps_the_mark(self):
        text = doctor.format_checks([doctor.Check("pi", WARN, "x")], color=True)
        self.assertTrue(text.startswith("\033[33m!\033[0m pi"))


class RunCommandTest(unittest.TestCase):
    def test_missing_program_does_not_raise(self):
        result = doctor.run_command(["placido-no-such-program"])
        self.assertEqual(result.code, 127)

    def test_output_is_captured(self):
        self.assertEqual(doctor.run_command(["echo", "hi"]), Result(0, "hi"))


if __name__ == "__main__":
    unittest.main()

