import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from placido import cli, config
from placido.config import AgentSpec, ConfigError


def load_text(text: str) -> config.Config:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(text)
        return config.load(path)


class DefaultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = config.load(None)

    def test_roles(self):
        roles = self.config.roles
        self.assertEqual(roles["spec"].agent, AgentSpec("claude", "opus", "xhigh"))
        self.assertEqual(roles["implement"].agent, AgentSpec("claude", "opus", "high"))
        self.assertEqual(roles["review"].agent, AgentSpec("codex", "gpt-6.1-sol", "max", "fast"))

    def test_fallback_chain_is_claude_then_deepseek(self):
        self.assertEqual(
            self.config.roles["review"].fallback,
            (
                AgentSpec("claude", "opus", "max"),
                AgentSpec("pi", "deepseek/deepseek-v4.1-flash", "xhigh"),
            ),
        )

    def test_settings(self):
        c = self.config
        self.assertEqual((c.base, c.setup, c.teardown), ("main", "", ""))
        self.assertEqual((c.commands, c.slice_gates, c.final_gates, c.last_gates), ({}, (), (), ()))
        self.assertEqual((c.test_first, c.plan_rounds, c.final_rounds), (True, 0, 3))
        self.assertEqual((c.stage_attempts, c.quota_wait), (3, 7200))
        self.assertIsNone(c.source)

    def test_empty_file_equals_defaults(self):
        loaded = load_text("")
        self.assertEqual(vars(loaded) | {"source": None}, vars(self.config))


class AgentArgsTest(unittest.TestCase):
    def test_claude(self):
        self.assertEqual(
            AgentSpec("claude", "opus", "xhigh").args(), ["--model", "opus", "--effort", "xhigh"]
        )

    def test_codex_always_names_its_tier(self):
        self.assertEqual(
            AgentSpec("codex", "gpt-6.1-sol", "max", "fast").args(),
            ["-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=max", "-c", "service_tier=fast"],
        )
        self.assertIn("service_tier=default", AgentSpec("codex", "gpt-6.1-sol", "max").args())

    def test_pi_always_names_openrouter(self):
        self.assertEqual(
            AgentSpec("pi", "deepseek/deepseek-v4.1-flash", "xhigh").args(),
            ["--provider", "openrouter", "--model", "deepseek/deepseek-v4.1-flash", "--thinking", "xhigh"],
        )

    def test_tier_is_ignored_outside_codex(self):
        self.assertNotIn("fast", " ".join(AgentSpec("claude", "opus", "high", "fast").args()))

    def test_command(self):
        self.assertEqual(AgentSpec("claude", "opus", "low").command(), "claude --model opus --effort low")


class OverrideTest(unittest.TestCase):
    def test_full_example(self):
        c = load_text(
            """
            [project]
            base     = "develop"
            setup    = "scripts/stack up"
            teardown = "scripts/stack down"

            [commands.unit]
            run   = "go test -short ./..."
            about = "about a minute"

            [commands.data]
            run = "make test-data"

            [gates]
            slice = ["unit"]
            final = ["unit", "data"]
            last  = ["data"]

            [roles.implement]
            model  = "sonnet"
            effort = "medium"

            [review]
            plan_rounds  = 1
            final_rounds = 2

            [implement]
            test_first = false

            [limits]
            stage_attempts = 5
            quota_wait     = "1h30m"
            """
        )
        self.assertEqual((c.base, c.setup, c.teardown), ("develop", "scripts/stack up", "scripts/stack down"))
        self.assertEqual(
            c.commands,
            {
                "unit": config.Command("unit", "go test -short ./...", "about a minute"),
                "data": config.Command("data", "make test-data"),
            },
        )
        self.assertEqual((c.slice_gates, c.final_gates, c.last_gates), (("unit",), ("unit", "data"), ("data",)))
        self.assertEqual(c.roles["implement"].agent, AgentSpec("claude", "sonnet", "medium"))
        self.assertEqual(c.roles["spec"].agent, AgentSpec("claude", "opus", "xhigh"))
        self.assertEqual((c.plan_rounds, c.final_rounds, c.test_first), (1, 2, False))
        self.assertEqual((c.stage_attempts, c.quota_wait), (5, 5400))

    def test_fix_follows_implement_unless_set(self):
        c = load_text('[roles.implement]\nmodel = "sonnet"\neffort = "medium"\n')
        self.assertEqual(c.roles["fix"].agent, AgentSpec("claude", "sonnet", "medium"))
        c = load_text('[roles.fix]\nagent = "codex"\n')
        self.assertEqual(c.roles["fix"].agent, AgentSpec("codex", "gpt-6.1-sol", "high"))
        self.assertEqual(c.roles["implement"].agent, AgentSpec("claude", "opus", "high"))

    def test_changing_the_agent_alone_picks_its_default_model(self):
        c = load_text('[roles.review]\nagent = "claude"\n')
        self.assertEqual(c.roles["review"].agent, AgentSpec("claude", "opus", "max", "fast"))
        c = load_text('[roles.spec]\nagent = "pi"\n')
        self.assertEqual(c.roles["spec"].agent.model, "deepseek/deepseek-v4.1-flash")

    def test_tier_can_be_turned_off(self):
        c = load_text('[roles.review]\ntier = "default"\n')
        self.assertEqual(c.roles["review"].agent.tier, "default")

    def test_global_fallback_replaces_the_chain(self):
        c = load_text('[[fallback]]\nagent = "pi"\nmodel = "x/y"\neffort = "low"\n')
        self.assertEqual(c.roles["spec"].fallback, (AgentSpec("pi", "x/y", "low"),))

    def test_fallback_entry_defaults(self):
        c = load_text('[[fallback]]\nagent = "claude"\n')
        self.assertEqual(c.roles["spec"].fallback, (AgentSpec("claude", "opus", "high"),))

    def test_empty_fallback_turns_it_off(self):
        c = load_text("fallback = []\n")
        self.assertEqual(c.roles["review"].fallback, ())

    def test_a_role_overrides_the_chain(self):
        c = load_text(
            '[[roles.review.fallback]]\nagent = "pi"\nmodel = "a/b"\neffort = "max"\n'
        )
        self.assertEqual(c.roles["review"].fallback, (AgentSpec("pi", "a/b", "max"),))
        self.assertEqual(len(c.roles["spec"].fallback), 2)


class ErrorTest(unittest.TestCase):
    def assertRejected(self, text: str, message: str) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_text(text)
        self.assertIn(message, str(caught.exception))

    def test_unknown_table(self):
        self.assertRejected("[rolez]\n", "config.rolez: unknown key")

    def test_unknown_role(self):
        self.assertRejected('[roles.plan]\nagent = "claude"\n', "roles.plan: unknown key")

    def test_unknown_role_key(self):
        self.assertRejected('[roles.spec]\nmodle = "opus"\n', "roles.spec.modle: unknown key")

    def test_fallback_entry_cannot_nest(self):
        self.assertRejected('[[fallback]]\nagent = "pi"\nfallback = []\n', "fallback #1.fallback: unknown key")

    def test_unknown_agent(self):
        self.assertRejected('[roles.spec]\nagent = "gemini"\n', "roles.spec.agent: 'gemini' is not one of")

    def test_unknown_effort(self):
        self.assertRejected('[roles.review]\neffort = "ultra"\n', "roles.review.effort: 'ultra'")

    def test_unknown_tier(self):
        self.assertRejected('[roles.review]\ntier = "priority"\n', "roles.review.tier: 'priority'")

    def test_fallback_needs_an_agent(self):
        self.assertRejected('[[fallback]]\nmodel = "x"\n', "fallback #1: needs an agent")

    def test_fallback_must_be_a_list(self):
        self.assertRejected('[fallback]\nagent = "pi"\n', "fallback: must be a list")

    def test_empty_model(self):
        self.assertRejected('[roles.spec]\nmodel = ""\n', "roles.spec.model: must be a model name")

    def test_wrong_types(self):
        self.assertRejected("[review]\nfinal_rounds = \"3\"\n", "review.final_rounds: must be int")
        self.assertRejected("[review]\nfinal_rounds = true\n", "review.final_rounds: must be int")
        self.assertRejected("[implement]\ntest_first = 1\n", "implement.test_first: must be bool")
        self.assertRejected("[project]\nsetup = 1\n", "project.setup: must be str")

    def test_old_check_and_test_keys(self):
        self.assertRejected('[project]\ncheck = "make check"\n', "project.check: unknown key")

    def test_bad_commands(self):
        self.assertRejected('[commands.Unit]\nrun = "x"\n', "commands.Unit: names use lowercase")
        self.assertRejected('[commands.unit]\nabout = "x"\n', "commands.unit.run: must be a command")
        self.assertRejected('[commands.unit]\nrun = "x"\nwhen = "y"\n', "commands.unit.when: unknown key")
        self.assertRejected('commands = ["x"]\n', "commands: must be a table")

    def test_gates_must_name_commands(self):
        self.assertRejected('[gates]\nslice = ["unit"]\n', "gates.slice: 'unit' is not a command (commands: none defined)")
        self.assertRejected(
            '[commands.unit]\nrun = "x"\n[gates]\nfinal = ["unit", "e2e"]\n',
            "gates.final: 'e2e' is not a command (commands: unit)",
        )
        self.assertRejected('[gates]\nslice = "unit"\n', "gates.slice: must be a list")
        self.assertRejected(
            '[commands.data]\nrun = "x"\n[gates]\nlast = ["nosuch mysql"]\n',
            "gates.last: 'nosuch mysql' names 'nosuch', which is not a command (commands: data)",
        )

    def test_a_gate_can_give_its_command_arguments(self):
        cfg = load_text('[commands.data]\nrun = "x --db"\n[gates]\nslice = ["data"]\nfinal = ["data mysql"]\n'
                   '[pr]\ngates = ["data postgres"]\n')
        self.assertEqual((cfg.slice_gates, cfg.final_gates, cfg.pr.gates), (("data",), ("data mysql",), ("data postgres",)))
        self.assertEqual(config.gate_command("data", cfg.commands), "x --db")
        self.assertEqual(config.gate_command("data mysql", cfg.commands), "x --db mysql")
        self.assertEqual(config.gate_command("""data 'a b' "it's" """, cfg.commands), """x --db 'a b' 'it'"'"'s'""")
        self.assertEqual(config.gate_log("data mysql"), "data-mysql")
        self.assertEqual(config.gate_log("data '../a b'"), "data--..-a-b-")
        self.assertEqual(config.gate_log("data-sqlite"), "data-sqlite")

    def test_negative_rounds(self):
        self.assertRejected("[review]\nfinal_rounds = -1\n", "must not be negative")

    def test_bad_duration(self):
        self.assertRejected('[limits]\nquota_wait = "2 hours"\n', "limits.quota_wait: '2 hours'")

    def test_bad_toml_names_the_file(self):
        self.assertRejected("[project\n", "config.toml:")


class CommandMenuTest(unittest.TestCase):
    def test_lists_each_command_with_its_description(self):
        c = load_text(
            '[commands.unit]\nrun = "go test ./..."\nabout = "about a minute"\n'
            '[commands.lint]\nrun = "make lint"\n'
        )
        self.assertEqual(
            config.command_menu(c),
            "## Commands for this project\n\n"
            "- **unit**, `go test ./...`: about a minute\n"
            "- **lint**, `make lint`\n",
        )

    def test_empty_without_commands(self):
        self.assertEqual(config.command_menu(config.load(None)), "")


class DurationTest(unittest.TestCase):
    def test_parse(self):
        for text, seconds in {"2h": 7200, "90m": 5400, "45s": 45, "1h30m": 5400, "0s": 0}.items():
            with self.subTest(text=text):
                self.assertEqual(config.parse_duration(text), seconds)

    def test_parse_rejects(self):
        for text in ("", "2", "h", "1d", "30m1h"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                config.parse_duration(text)

    def test_format(self):
        self.assertEqual(config.format_duration(5400), "1h30m")
        self.assertEqual(config.format_duration(0), "0s")


class FindConfigTest(unittest.TestCase):
    def test_found_in_a_parent_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".placido").mkdir()
            (root / ".placido" / "config.toml").write_text("")
            (root / "src" / "deep").mkdir(parents=True)
            self.assertEqual(config.find_config(root / "src" / "deep"), root / ".placido" / "config.toml")

    def test_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(config.find_config(Path(tmp)))


class PRSettingsTest(unittest.TestCase):
    def test_defaults(self):
        c = config.load(None)
        self.assertEqual(
            [r.agent for r in c.pr.reviewers],
            [AgentSpec("claude", "opus", "xhigh"), AgentSpec("codex", "gpt-6.1-sol", "max", "fast")],
        )
        self.assertEqual(c.pr.reviewers[1].fallback, c.roles["review"].fallback)
        self.assertEqual(c.pr.host_paths, (".placido", ".claude", ".codex", ".mcp.json"))
        self.assertEqual(c.pr.gates, ())
        self.assertEqual(c.roles["scan"].agent, AgentSpec("claude", "opus", "high"))
        self.assertEqual(c.roles["judge"].agent, AgentSpec("claude", "opus", "high"))
        self.assertEqual(c.roles["decide"].agent, c.roles["judge"].agent)
        self.assertEqual((c.pr.parallel, c.pr.fix_rounds), (True, 3))
        self.assertNotIn("verify", c.roles)  # the fixes are reviewed in full, in rounds of their own

    def test_gates_default_to_the_slice_and_final_gates(self):
        c = load_text(
            '[commands.unit]\nrun = "u"\n[commands.lint]\nrun = "l"\n'
            '[gates]\nslice = ["unit"]\nfinal = ["lint", "unit"]\n'
        )
        self.assertEqual(c.pr.gates, ("unit", "lint"))

    def test_everything_is_configurable(self):
        c = load_text(
            '[commands.unit]\nrun = "u"\n'
            '[pr]\ngates = ["unit"]\nhost_paths = ["scripts/", ".placido"]\nfix_rounds = 5\n'
            'reviewers = [{agent = "codex", effort = "high", fallback = []}, {agent = "pi"}]\n'
            '[roles.judge]\nagent = "codex"\n'
        )
        self.assertEqual([r.agent for r in c.pr.reviewers],
                         [AgentSpec("codex", "gpt-6.1-sol", "high"), AgentSpec("pi", "deepseek/deepseek-v4.1-flash", "high")])
        self.assertEqual(c.pr.reviewers[0].fallback, ())
        self.assertEqual((c.pr.gates, c.pr.host_paths, c.pr.fix_rounds), (("unit",), ("scripts", ".placido"), 5))
        self.assertEqual(c.roles["judge"].agent.agent, "codex")
        self.assertEqual(c.roles["decide"].agent.agent, "codex")  # follows the judge unless set itself

    def test_refusals(self):
        cases = {
            "[pr]\nreviewers = []\n": "pr.reviewers: must be a list of 1 to 26 agent tables",
            "[pr]\nreviewers = [{model = \"x\"}]\n": "pr.reviewers #1: needs an agent",
            "[pr]\ngates = [\"nope\"]\n": "pr.gates: 'nope' is not a command",
            "[pr]\nhost_paths = [\"/etc\"]\n": "pr.host_paths: '/etc' must be a path inside the repository",
            "[pr]\nhost_paths = [\"a/../../b\"]\n": "must be a path inside the repository",
            "[pr]\ncontributor_share = 0.5\n": "pr.contributor_share: unknown key",
            "[roles.verify]\nagent = \"codex\"\n": "roles.verify: unknown key",
            "[pr]\nparallel = 1\n": "pr.parallel: must be bool",
            "[pr]\nfix_rounds = -1\n": "pr.fix_rounds: must not be negative",
            "[pr]\nfix_rounds = 0\n": "pr.fix_rounds: must be at least 1",
        }
        for text, message in cases.items():
            with self.assertRaisesRegex(ConfigError, message, msg=text):
                load_text(text)

    def test_described(self):
        text = config.describe(config.load(None), Path("."))
        self.assertIn("[pr]\nreviewer A claude --model opus --effort xhigh", text)
        self.assertIn("reviewer B codex -m gpt-6.1-sol", text)
        self.assertIn("host paths .placido, .claude, .codex, .mcp.json", text)


class ConfigCommandTest(unittest.TestCase):
    def placido(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_shows_the_resolved_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".placido").mkdir()
            (Path(tmp) / ".placido" / "config.toml").write_text(
                '[project]\nsetup = "make setup"\n[commands.unit]\nrun = "make unit"\n[gates]\nslice = ["unit"]\n'
            )
            code, out, _ = self.placido("config", "--project", tmp)
        self.assertEqual(code, 0)
        self.assertIn(f"config  {Path(tmp).resolve()}/.placido/config.toml", out)
        self.assertIn("setup    make setup", out)
        self.assertIn("unit  make unit", out)
        self.assertIn("slice  unit", out)
        self.assertIn(
            "review     codex -m gpt-6.1-sol -c model_reasoning_effort=max -c service_tier=fast", out
        )
        self.assertIn("  then     pi --provider openrouter", out)
        self.assertIn("limits.quota_wait      2h", out)

    def test_defaults_when_there_is_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.placido("config", "--project", tmp)
        self.assertEqual(code, 0)
        self.assertIn("built-in defaults (no .placido/config.toml in", out)

    def test_invalid_config_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".placido").mkdir()
            (Path(tmp) / ".placido" / "config.toml").write_text('[roles.spec]\neffort = "huge"\n')
            code, _, err = self.placido("config", "--project", tmp)
        self.assertEqual(code, 1)
        self.assertIn("roles.spec.effort: 'huge'", err)


if __name__ == "__main__":
    unittest.main()
