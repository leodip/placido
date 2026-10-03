"""Placido's tests. None of them may reach the user's real Herdr: a test that does
would open workspaces, tabs, and agents in the user's own session (it happened on
2026-10-02, when four temporary repositories appeared as workspaces). Every Herdr
made without an explicit runner refuses to run. Likewise no test may send an email,
or read the user's own ~/.config/placido/config.toml."""

import os

from placido import alerts, herdr


def _refuse(argv, timeout=None):
    raise AssertionError(f"a test reached the real herdr: {' '.join(argv)}")


def _refuse_email(key, payload):
    raise AssertionError(f"a test tried to send an email: {payload.get('subject')}")


herdr.REAL_RUNNER[0] = _refuse
alerts.SENDER[0] = _refuse_email
os.environ["PLACIDO_USER_CONFIG"] = os.path.join(os.path.dirname(__file__), "no-user-config.toml")
