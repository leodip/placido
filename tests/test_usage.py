import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from placido import usage

START = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 18, 10, tzinfo=timezone.utc)


def at(minute: int) -> str:
    return f"2026-10-02T18:{minute:02d}:00.000Z"


class UsageCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "transcript.jsonl"

    def read(self, agent: str, *events: dict) -> usage.Usage:
        self.path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return usage.read(self.path, agent, START, END)


def reply(minute: int, message_id: str, **counts) -> dict:
    return {"type": "assistant", "timestamp": at(minute), "message": {"id": message_id, "usage": counts}}


class ClaudeTest(UsageCase):
    def test_each_reply_counts_once_inside_the_window(self):
        found = self.read(
            "claude",
            reply(0, "early", input_tokens=999) | {"timestamp": "2026-10-02T17:59:00Z"},  # before the step
            reply(1, "a", input_tokens=2, cache_creation_input_tokens=100, cache_read_input_tokens=1000,
                  output_tokens=50, output_tokens_details={"thinking_tokens": 20}),
            reply(1, "a", input_tokens=2, cache_creation_input_tokens=100, cache_read_input_tokens=1000,
                  output_tokens=50, output_tokens_details={"thinking_tokens": 20}),  # the same reply again
            reply(2, "b", input_tokens=3, cache_read_input_tokens=1100, output_tokens=10),
            reply(11, "late", input_tokens=999),
        )
        self.assertEqual(
            (found.input, found.cache_write, found.cache_read, found.output, found.reasoning),
            (5, 100, 2100, 60, 20),
        )
        self.assertEqual(found.total, 2265)
        self.assertEqual(found.agents, {"claude"})

    def test_api_errors_are_not_usage(self):
        error = reply(1, "x", input_tokens=5) | {"isApiErrorMessage": True}
        self.assertEqual(self.read("claude", error).total, 0)


def tokens(minute: int, inputs: int, cached: int, output: int, used: float | None = None) -> dict:
    payload = {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": output,
        "reasoning_output_tokens": output // 2,
    }}}
    if used is not None:
        payload["rate_limits"] = {"primary": {"used_percent": used, "window_minutes": 10080}}
    return {"timestamp": at(minute) if minute >= 0 else "2026-10-02T17:50:00Z", "type": "event_msg", "payload": payload}


class CodexTest(UsageCase):
    def test_a_resumed_session_counts_only_its_own_round(self):
        found = self.read(
            "codex",
            tokens(-1, 1000, 800, 100, used=40.0),  # an earlier round of the same session
            tokens(3, 1500, 1200, 150, used=41.0),
            tokens(9, 3000, 2600, 300, used=42.0),
            tokens(12, 9999, 9999, 999, used=50.0),  # after the step
        )
        self.assertEqual((found.input, found.cache_read, found.output, found.reasoning), (200, 1800, 200, 100))
        self.assertEqual((found.window_first, found.window_last, found.window_minutes), (41.0, 42.0, 10080))

    def test_a_fresh_session(self):
        found = self.read("codex", tokens(2, 500, 300, 40))
        self.assertEqual((found.input, found.cache_read, found.output), (200, 300, 40))
        self.assertIsNone(found.window_last)


class PiTest(UsageCase):
    def test_tokens_and_real_cost(self):
        message = {"type": "message", "timestamp": at(4), "message": {"role": "assistant", "usage": {
            "input": 1452, "output": 18, "cacheRead": 256, "cacheWrite": 0, "reasoning": 14,
            "cost": {"total": 0.25}}}}
        found = self.read("pi", message, message | {"timestamp": at(5)}, {"type": "message", "message": {"role": "user"}})
        self.assertEqual((found.input, found.cache_read, found.output, found.reasoning), (2904, 512, 36, 28))
        self.assertAlmostEqual(found.cost, 0.5)


class AddTest(unittest.TestCase):
    def test_keeps_the_first_and_last_window_readings(self):
        a = usage.Usage(input=1, window_first=40.0, window_last=41.0, window_minutes=10080, agents={"codex"})
        b = usage.Usage(output=2, window_first=41.0, window_last=43.0, agents={"claude"})
        both = a.add(b)
        self.assertEqual((both.input, both.output, both.window_first, both.window_last), (1, 2, 40.0, 43.0))
        self.assertEqual(both.agents, {"codex", "claude"})

    def test_missing_transcript(self):
        self.assertEqual(usage.read(Path("/nonexistent"), "claude", START, END).total, 0)
