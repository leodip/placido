import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from placido import outcomes
from placido.outcomes import Failure

# 2026-10-02 12:00 in São Paulo (UTC-3), a Friday.
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


def at(hour: int, minute: int = 0, day: int = 2, tz: str = "America/Sao_Paulo") -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


class TranscriptCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "transcript.jsonl"

    def failure(self, agent: str, *events: dict) -> Failure | None:
        self.path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return outcomes.last_failure(self.path, agent, NOW)


# ---- codex: the persisted task_complete carries the turn's error ----------------------


def codex_end(error: dict | None = None) -> dict:
    payload = {"type": "task_complete", "turn_id": "t1", "last_agent_message": None}
    if error is not None:
        payload["error"] = error
    return {"timestamp": "2026-10-02T15:00:00Z", "type": "event_msg", "payload": payload}


def codex_limits(used: float, resets_at: int) -> dict:
    return {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
        "limit_id": "codex", "primary": {"used_percent": used, "window_minutes": 300, "resets_at": resets_at},
        "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": resets_at + 99999},
    }}}


USAGE_LIMIT = (
    "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more"
    " credits or try again at 3:41 PM."
)


class CodexTest(TranscriptCase):
    def test_a_successful_turn(self):
        self.assertIsNone(self.failure("codex", codex_end()))

    def test_an_interrupted_turn_is_not_a_failure(self):
        aborted = {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}}
        self.assertIsNone(self.failure("codex", aborted))

    def test_only_the_last_turn_counts(self):
        earlier = codex_end({"message": "boom", "codex_error_info": "server_overloaded"})
        self.assertIsNone(self.failure("codex", earlier, codex_end()))

    def test_cyber_refusal(self):
        found = self.failure("codex", codex_end({
            "message": "This request has been flagged for possible cybersecurity risk.",
            "codex_error_info": "cyber_policy",
        }))
        self.assertEqual(found.kind, "refusal")
        self.assertIn("cybersecurity", found.message)

    def test_other_policies_are_refusals(self):
        for info in ("bio_policy", "misalignment_policy_violation", "invalid_prompt"):
            found = self.failure("codex", codex_end({"message": "Invalid request.", "codex_error_info": info}))
            self.assertEqual(found.kind, "refusal", info)

    def test_usage_limit_takes_its_reset_from_the_exhausted_window(self):
        resets = int(at(14, 30))
        found = self.failure(
            "codex", codex_limits(100.0, resets),
            codex_end({"message": USAGE_LIMIT, "codex_error_info": "usage_limit_exceeded"}),
        )
        self.assertEqual((found.kind, found.resets_at), ("quota", float(resets)))

    def test_usage_limit_without_a_snapshot_reads_the_message(self):
        found = self.failure("codex", codex_end({"message": USAGE_LIMIT, "codex_error_info": "usage_limit_exceeded"}))
        self.assertEqual((found.kind, found.resets_at), ("quota", at(15, 41)))

    def test_context_window(self):
        found = self.failure("codex", codex_end({
            "message": "Codex ran out of room in the model's context window. Start a new thread.",
            "codex_error_info": "context_window_exceeded",
        }))
        self.assertEqual(found.kind, "context")

    def test_variants_with_fields(self):
        found = self.failure("codex", codex_end({
            "message": "unexpected status 503 Service Unavailable: busy",
            "codex_error_info": {"http_connection_failed": {"http_status_code": 503}},
        }))
        self.assertEqual(found.kind, "transient")

    def test_a_401_is_told_by_its_text(self):
        found = self.failure("codex", codex_end({
            "message": "unexpected status 401 Unauthorized: token expired",
            "codex_error_info": {"http_connection_failed": {"http_status_code": 401}},
        }))
        self.assertEqual(found.kind, "auth")

    def test_a_dropped_stream_after_retries(self):
        found = self.failure("codex", codex_end({
            "message": "stream disconnected before completion: request timed out", "codex_error_info": "other",
        }))
        self.assertEqual(found.kind, "transient")

    def test_unauthorized(self):
        found = self.failure("codex", codex_end({
            "message": "Your access token could not be refreshed. Please log out and sign in again.",
            "codex_error_info": "unauthorized",
        }))
        self.assertEqual(found.kind, "auth")


# ---- claude: an "API Error" assistant entry ------------------------------------------


def claude_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def claude_error(text: str, error: str, stop_reason: str | None = None, **extra) -> dict:
    entry = claude_text(text)
    entry.update(isApiErrorMessage=True, error=error, **extra)
    entry["message"]["stop_reason"] = stop_reason
    return entry


class ClaudeTest(TranscriptCase):
    def test_a_successful_turn(self):
        self.assertIsNone(self.failure("claude", claude_text("Done."), {"type": "system", "subtype": "x"}))

    def test_an_error_followed_by_a_good_answer_is_history(self):
        overloaded = claude_error("API Error: Repeated 529 Overloaded errors", "server_error")
        self.assertIsNone(self.failure("claude", overloaded, claude_text("Done.")))

    def test_refusal(self):
        found = self.failure("claude", claude_error(
            "API Error: Opus's safeguards flagged this message (https://www.anthropic.com/legal/aup).",
            "invalid_request", stop_reason="refusal",
        ))
        self.assertEqual(found.kind, "refusal")

    def test_session_limit_with_its_stored_reset(self):
        resets = int(at(15))
        found = self.failure("claude", claude_error(
            "You've hit your session limit · resets 3pm (America/Sao_Paulo)", "rate_limit",
            quotaLimits={"five_hour": {"utilization": 1.0, "resetsAt": resets}},
        ))
        self.assertEqual((found.kind, found.resets_at), ("quota", float(resets)))

    def test_session_limit_read_from_the_text_in_its_own_zone(self):
        # Noon in São Paulo is 5pm in Rome, so 3pm there is tomorrow's.
        found = self.failure("claude", claude_error(
            "You've hit your session limit · resets 3pm (Europe/Rome)", "rate_limit",
        ))
        self.assertEqual(found.resets_at, at(15, day=3, tz="Europe/Rome"))
        found = self.failure("claude", claude_error(
            "You've hit your session limit · resets 7pm (Europe/Rome)", "rate_limit",
        ))
        self.assertEqual(found.resets_at, at(19, tz="Europe/Rome"))

    def test_a_429_without_a_limit_is_transient(self):
        found = self.failure("claude", claude_error(
            "API Error: Request rejected (429) · this may be a temporary capacity issue.", "rate_limit",
        ))
        self.assertEqual(found.kind, "transient")

    def test_login_expired(self):
        found = self.failure("claude", claude_error("Login expired · Please run /login", "authentication_failed"))
        self.assertEqual(found.kind, "auth")

    def test_a_temporary_authentication_error(self):
        found = self.failure("claude", claude_error(
            "Authentication error · This may be a temporary network issue, please try again",
            "authentication_failed",
        ))
        self.assertEqual(found.kind, "transient")

    def test_prompt_too_long(self):
        found = self.failure("claude", claude_error("Prompt is too long", "invalid_request"))
        self.assertEqual(found.kind, "context")

    def test_overloaded(self):
        found = self.failure("claude", claude_error(
            "API Error: Repeated 529 Overloaded errors · The API is at capacity", "server_error",
        ))
        self.assertEqual(found.kind, "transient")


# ---- pi: the turn's message ends with stopReason "error" -----------------------------


def pi_message(entry_id: str, stop: str = "stop", error: str | None = None) -> dict:
    message = {"role": "assistant", "content": [], "stopReason": stop, "timestamp": int(NOW.timestamp() * 1000)}
    if error is not None:
        message["errorMessage"] = error
    return {"type": "message", "id": entry_id, "message": message}


class PiTest(TranscriptCase):
    def test_a_successful_turn(self):
        self.assertIsNone(self.failure("pi", pi_message("a")))

    def test_a_retried_error_is_history(self):
        failed = pi_message("a", "error", "529 overloaded")
        edit = {"type": "context_edit", "targetId": "a", "replacement": None}
        self.assertIsNone(self.failure("pi", failed, edit))

    def test_the_final_error(self):
        found = self.failure("pi", pi_message("a", "error", "529 overloaded"))
        self.assertEqual(found, Failure("transient", "529 overloaded"))

    def test_moderation(self):
        found = self.failure("pi", pi_message("a", "error", "403 Your input was flagged by moderation"))
        self.assertEqual(found.kind, "refusal")

    def test_openrouter_out_of_credits(self):
        found = self.failure("pi", pi_message("a", "error", "402 This request requires more credits"))
        self.assertEqual(found.kind, "quota")

    def test_codex_subscription_limit_with_relative_reset(self):
        found = self.failure("pi", pi_message(
            "a", "error", "You have hit your ChatGPT usage limit (plus plan). Try again in ~25 min.",
        ))
        self.assertEqual((found.kind, found.resets_at), ("quota", NOW.timestamp() + 25 * 60))

    def test_aborted_is_not_a_failure(self):
        self.assertIsNone(self.failure("pi", pi_message("a", "aborted", "Request was aborted")))


class MissingTest(TranscriptCase):
    def test_no_file(self):
        self.assertIsNone(outcomes.last_failure(self.path, "codex", NOW))

    def test_unknown_agent(self):
        self.assertIsNone(self.failure("other", codex_end({"message": "x"})))

    def test_broken_lines_are_skipped(self):
        self.path.write_text("not json\n" + json.dumps(pi_message("a", "error", "terminated")) + "\n")
        self.assertEqual(outcomes.last_failure(self.path, "pi", NOW).kind, "transient")


class ResetTest(unittest.TestCase):
    def test_codex_clock_later_today(self):
        self.assertEqual(outcomes.parse_reset("try again at 3:41 PM.", NOW), at(15, 41))

    def test_a_clock_already_past_means_tomorrow(self):
        self.assertEqual(outcomes.parse_reset("try again at 9:05 AM.", NOW), at(9, 5, day=3))

    def test_codex_date(self):
        self.assertEqual(outcomes.parse_reset("try again at Oct 5th, 2026 3:41 PM.", NOW), at(15, 41, day=5))

    def test_claude_date_and_zone(self):
        self.assertEqual(
            outcomes.parse_reset("resets Oct 5, 3pm (Europe/Rome)", NOW), at(15, day=5, tz="Europe/Rome"),
        )

    def test_unknown_zone_uses_local_time(self):
        self.assertEqual(outcomes.parse_reset("resets 3pm (Mars/Olympus)", NOW), at(15))

    def test_nothing_to_read(self):
        self.assertIsNone(outcomes.parse_reset("try again later.", NOW))


class ClassifyTest(unittest.TestCase):
    def test_a_limit_named_with_a_429_is_quota(self):
        self.assertEqual(outcomes.classify("429 You've hit your usage limit"), "quota")

    def test_plain_statuses(self):
        self.assertEqual(outcomes.classify("HTTP 502"), "transient")
        self.assertEqual(outcomes.classify("HTTP 401"), "auth")
        self.assertEqual(outcomes.classify("something odd"), "error")


class ScreenTest(unittest.TestCase):
    def test_codex_error_line(self):
        screen = "• Ran tests\n\n■ You’ve hit your usage limit. Try again at 3:41 PM.\n\n› \n"
        found = outcomes.screen_failure(screen, NOW)
        self.assertEqual((found.kind, found.resets_at), ("quota", at(15, 41)))

    def test_codex_refusal_card(self):
        found = outcomes.screen_failure("ⓘ This content can’t be shown\nWe take extra care with some requests.\n")
        self.assertEqual(found.kind, "refusal")

    def test_claude_api_error(self):
        found = outcomes.screen_failure("⎿  API Error: Repeated 529 Overloaded errors\n> \n")
        self.assertEqual(found.kind, "transient")

    def test_ordinary_output_mentioning_errors_is_not_a_failure(self):
        screen = "AssertionError: 1 != 2\nFAILED (failures=1)\nI'll fix the test.\n> \n"
        self.assertIsNone(outcomes.screen_failure(screen))


if __name__ == "__main__":
    unittest.main()
