"""Placido's tests. None of them may reach the user's real Herdr: a test that does
would open workspaces, tabs, and agents in the user's own session (it happened on
2026-10-02, when four temporary repositories appeared as workspaces). Every Herdr
made without an explicit runner refuses to run."""

from placido import herdr


def _refuse(argv, timeout=None):
    raise AssertionError(f"a test reached the real herdr: {' '.join(argv)}")


herdr.REAL_RUNNER[0] = _refuse
