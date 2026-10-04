import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from placido import alerts, config, quota, report, runlog, status
from placido.herdr import Herdr
from placido.proc import Result


class UserConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"

    def load(self, text: str) -> config.UserConfig:
        self.path.write_text(text)
        return config.load_user(self.path)

    def test_email_is_off_without_the_file(self):
        self.assertFalse(config.load_user(self.path).email.enabled)

    def test_email_settings(self):
        found = self.load('[email]\nenabled = true\nto = "a@b.c"\nfrom = "placido <p@b.c>"\napi_key_file = "~/k"\n')
        self.assertEqual(found.email, config.Email(True, "a@b.c", "placido <p@b.c>", "~/k"))

    def test_enabled_needs_every_field(self):
        with self.assertRaisesRegex(config.ConfigError, "email: enabled needs from, api_key_file"):
            self.load('[email]\nenabled = true\nto = "a@b.c"\n')

    def test_typos_and_wrong_types_are_errors(self):
        with self.assertRaisesRegex(config.ConfigError, "email.too: unknown key"):
            self.load('[email]\ntoo = "a@b.c"\n')
        with self.assertRaisesRegex(config.ConfigError, "email.enabled: must be bool"):
            self.load('[email]\nenabled = "yes"\n')
        with self.assertRaisesRegex(config.ConfigError, "user config.mail: unknown key"):
            self.load('[mail]\nenabled = true\n')


class EmailCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.run = runlog.Run.create(self.tmp / "runs", "goiabada", "404-login-email", {
            "project": "goiabada", "issue_number": 404, "issue_title": "Security: the login email",
            "issue_source": "https://github.com/leodip/goiabada/issues/404",
        })
        self.key = self.tmp / "resend"
        self.key.write_text("re_secret\n")
        self.sent: list[tuple[str, dict]] = []
        patch = mock.patch.object(alerts, "SENDER", [lambda key, payload: self.sent.append((key, payload))])
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("claude", "codex"):
            p = mock.patch.object(quota, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def settings(self, enabled: bool = True, key: Path | None = None) -> config.UserConfig:
        return config.UserConfig(
            config.Email(enabled, "leodip@outlook.com", "placido <contact@leodip.com>", str(key or self.key)), None
        )

    def events(self, kind: str) -> list[dict]:
        return [e for e in runlog.read_events(self.run.path) if e["event"] == kind]


class EmailTest(EmailCase):
    def test_sends_through_resend_with_the_key_from_its_file(self):
        alerts.email(self.run, "02-spec asks you", "Which store? · answer in the 02-spec tab", self.settings())
        ((key, payload),) = self.sent
        self.assertEqual(key, "re_secret")
        self.assertEqual(payload["from"], "placido <contact@leodip.com>")
        self.assertEqual(payload["to"], ["leodip@outlook.com"])
        self.assertEqual(payload["subject"], "placido · goiabada #404: 02-spec asks you")
        self.assertIn("<h2", payload["html"])
        (sent,) = self.events("email.sent")
        self.assertNotIn("re_secret", json.dumps(sent))  # the key is never logged

    def test_off_sends_nothing(self):
        alerts.email(self.run, "t", "b", self.settings(enabled=False))
        self.assertEqual(self.sent, [])

    def test_a_failure_is_logged_and_never_raised(self):
        alerts.email(self.run, "t", "b", self.settings(key=self.tmp / "missing"))
        self.assertIn("missing", self.events("email.failed")[0]["error"])
        def broken(key, payload):
            raise OSError("Resend answered 401: invalid key")
        with mock.patch.object(alerts, "SENDER", [broken]):
            alerts.email(self.run, "t", "b", self.settings())
        self.assertIn("401", self.events("email.failed")[-1]["error"])

    def test_notify_shows_and_emails(self):
        calls = []
        herdr = Herdr(lambda argv, timeout=None: calls.append(argv) or Result(0, ""))
        with mock.patch.object(config, "load_user", return_value=self.settings()):
            alerts.notify(self.run, herdr, "setup failed", "see setup.log")
        self.assertEqual(calls[0][1:5], ["notification", "show", "placido · 404-login-email: setup failed", "--body"])
        self.assertEqual(self.sent[0][1]["subject"], "placido · goiabada #404: setup failed")

    def test_the_message(self):
        status.show(self.run.path, "you · answer in 02-spec")
        self.run.event("pr.opened", url="https://github.com/leodip/goiabada/pull/466", number=466)
        readings = {
            "claude": quota.Reading("claude", (quota.Window("5h", 22.4, 1_009_000.0), quota.Window("7d", 57, None)), 999_700.0),
            "codex": quota.Reading("codex", (quota.Window("7d", 9, 1_000_000.0 + 5 * 86400),), None),
        }
        with mock.patch.object(quota, "claude", return_value=readings["claude"]), \
                mock.patch.object(quota, "codex", return_value=readings["codex"]):
            text = alerts.render(self.run.path, "02-spec asks <you>", "Which store?", now=1_000_000.0)
        self.assertIn("02-spec asks &lt;you&gt;", text)
        self.assertIn('<a href="https://github.com/leodip/goiabada/issues/404">Security: the login email</a>', text)
        self.assertIn("you · answer in 02-spec", text)
        self.assertIn('<a href="https://github.com/leodip/goiabada/pull/466">', text)
        self.assertIn("5h: <b>22%</b> used, resets in 2h 30m · 7d: <b>57%</b> used", text)
        self.assertNotIn("as of", text)  # a reading's age only cluttered the email
        self.assertNotIn("Time spent", text)
        self.assertIn("7d: <b>9%</b> used, resets in 5d 0h", text)

    def test_no_quota_reading_yet(self):
        self.assertIn("No reading yet.", alerts.render(self.run.path, "t", "b"))


class QuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_claude_from_the_status_line_s_file(self):
        path = self.tmp / "claude-quota.json"
        path.write_text(json.dumps({"saved_at": 1000, "rate_limits": {
            "five_hour": {"used_percentage": 22.0, "resets_at": 5000},
            "seven_day": {"used_percentage": 57.0, "resets_at": 9000},
        }}))
        reading = quota.claude(path)
        self.assertEqual([(w.label, w.used, w.resets_at) for w in reading.windows],
                         [("5h", 22.0, 5000.0), ("7d", 57.0, 9000.0)])
        self.assertEqual(reading.as_of, 1000.0)
        self.assertIsNone(quota.claude(self.tmp / "missing.json"))

    def test_codex_from_its_newest_session(self):
        day = self.tmp / "2026" / "10" / "03"
        day.mkdir(parents=True)
        old = day / "rollout-old.jsonl"
        old.write_text(json.dumps({"timestamp": "2026-10-03T01:00:00Z", "payload": {"type": "token_count",
            "rate_limits": {"primary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 100}}}}) + "\n")
        new = day / "rollout-new.jsonl"
        new.write_text(
            json.dumps({"timestamp": "2026-10-03T03:00:00Z", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 9.0, "window_minutes": 10080, "resets_at": 200},
                "secondary": {"used_percent": 30.0, "window_minutes": 300, "resets_at": 50}}}}) + "\n"
            + json.dumps({"timestamp": "2026-10-03T03:01:00Z", "payload": {"type": "agent_message"}}) + "\n"
        )
        os.utime(old, (1, 1))
        reading = quota.codex(self.tmp)
        self.assertEqual([(w.label, w.used) for w in reading.windows], [("7d", 9.0), ("5h", 30.0)])
        self.assertEqual(reading.as_of, 1790996400.0)

    def test_no_sessions(self):
        self.assertIsNone(quota.codex(self.tmp / "none"))


class TestEmailTest(EmailCase):
    def test_a_test_email_carries_the_quota(self):
        self.assertEqual(alerts.test(self.settings()), "sent to leodip@outlook.com")
        self.assertEqual(self.sent[0][1]["subject"], "placido · test email")
        self.assertIn("No reading yet.", self.sent[0][1]["html"])
        self.assertIn("email is off", alerts.test(self.settings(enabled=False)))

    def test_the_doctor_reports_email(self):
        from placido import doctor
        self.assertEqual(doctor.check_email().detail[:3], "off")
        cfg = self.tmp / "user.toml"
        cfg.write_text(f'[email]\nenabled = true\nto = "a@b.c"\nfrom = "p <p@b.c>"\napi_key_file = "{self.tmp / "nokey"}"\n')
        with mock.patch.dict(os.environ, {"PLACIDO_USER_CONFIG": str(cfg)}):
            self.assertIn("no Resend key", doctor.check_email().detail)
            cfg.write_text(cfg.read_text().replace(str(self.tmp / "nokey"), str(self.key)))
            self.assertIn("Claude's quota is not saved yet", doctor.check_email().detail)


class TimesTest(EmailCase):
    """The "pull request is ready" email says how long each stage took."""

    def test_stages_from_the_event_log(self):
        def at(minute: int, kind: str, **fields) -> None:
            line = {"ts": f"2026-10-03T10:{minute:02d}:00.000-03:00", "event": kind, **fields}
            with (self.run.path / "events.jsonl").open("a") as out:
                out.write(json.dumps(line) + "\n")
        at(0, "step.start", step="01-spec", role="spec")
        at(20, "step.end", step="01-spec")
        at(21, "slice.start", slice=1)
        at(22, "step.start", step="02-implement-slice-1", role="implement")
        at(29, "gate.start", gate="unit")  # a slice gate, inside the step
        at(30, "step.end", step="02-implement-slice-1")
        at(30, "slice.failed", slice=1)
        at(31, "slice.start", slice=1)
        at(36, "slice.committed", slice=1)
        at(37, "gate.start", gate="lint")  # the final gates, outside any step
        at(40, "final_gates.passed", attempt=1)
        at(40, "step.start", step="04-review-1", role="review")
        at(49, "step.end", step="04-review-1")
        at(50, "step.start", step="05-followups", role="fix")
        at(52, "step.end", step="05-followups")
        at(52, "github.pushed")
        at(59, "ci.result", outcome="green", attempt=1)
        self.assertEqual(report.stages(self.run.path), [
            ("Interview (with you)", 1200.0), ("Slice 1", 900.0), ("Final gates", 180.0),
            ("Review round 1", 540.0), ("Follow-ups", 120.0), ("CI", 420.0),
        ])
        run_start = report.when(next(runlog.read_events(self.run.path))["ts"]).timestamp()
        text = alerts.render(self.run.path, "the pull request is ready", "CI green", now=run_start + 3600, times=True)
        self.assertIn("Time spent", text)
        self.assertIn('>Slice 1</td><td style="padding:4px 12px 4px 0;vertical-align:top;text-align:right">15m 00s<', text)
        # The total leaves out the 20-minute interview and the pause before the run:
        # from the first slice's start at 10:21 to the email at 11:00.
        self.assertIn("<b>Total, unattended</b>", text)
        self.assertIn("<b>39m 00s</b>", text)

    def test_pr_ready_asks_for_the_times(self):
        self.run.event("run.start")
        self.run.event("slice.start", slice=1)
        with mock.patch.object(config, "load_user", return_value=self.settings()):
            alerts.notify(self.run, Herdr(lambda argv, timeout=None: Result(0, "")), "the pull request is ready",
                          "CI green", times=True)
        self.assertIn("Time spent", self.sent[0][1]["html"])
