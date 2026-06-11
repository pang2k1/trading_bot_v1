"""test_decision_engine.py — table-driven test of _decide_action."""

import pytest

import config
from live_trader import _decide_action


# Helper to build signal dicts
def sig(long_entry=False, long_exit=False, short_entry=False, short_exit=False):
    return {
        "long_entry": long_entry,
        "long_exit": long_exit,
        "short_entry": short_entry,
        "short_exit": short_exit,
    }


class TestManageLongPosition:
    """When holding a long, test exit conditions."""

    def test_hold_on_no_signal(self):
        action, _ = _decide_action(sig(), 0.0, "long")
        assert action == "hold"

    def test_exit_on_technical_long_exit(self):
        action, _ = _decide_action(sig(long_exit=True), 0.0, "long")
        assert action == "close_long"

    def test_exit_on_strongly_bearish_news(self):
        action, _ = _decide_action(sig(), -0.50, "long")
        assert action == "close_long"

    def test_hold_on_weakly_bearish_news(self):
        action, _ = _decide_action(sig(), -0.10, "long")
        assert action == "hold"


class TestManageShortPosition:
    """When holding a short, test exit conditions."""

    def test_hold_on_no_signal(self):
        action, _ = _decide_action(sig(), 0.0, "short")
        assert action == "hold"

    def test_exit_on_technical_short_exit(self):
        action, _ = _decide_action(sig(short_exit=True), 0.0, "short")
        assert action == "close_short"

    def test_exit_on_strongly_bullish_news(self):
        action, _ = _decide_action(sig(), 0.50, "short")
        assert action == "close_short"

    def test_hold_on_weakly_bullish_news(self):
        action, _ = _decide_action(sig(), 0.10, "short")
        assert action == "hold"


class TestNoPosition:
    """When flat, test entry conditions."""

    @pytest.fixture(autouse=True)
    def _ensure_long_only_false(self):
        config.LONG_ONLY = False

    def test_skip_when_no_signal_neutral_news(self):
        action, _ = _decide_action(sig(), 0.0, None)
        assert action == "skip"

    def test_open_long_on_strong_bull_news(self):
        """Strongly bullish news opens long without technical signal."""
        action, _ = _decide_action(sig(), config.NEWS_STRONG_BULL, None)
        assert action == "open_long"

    def test_open_long_on_weak_bull_plus_technical(self):
        """Weak bull + technical long = double confirmation."""
        action, _ = _decide_action(
            sig(long_entry=True), config.NEWS_WEAK_BULL, None
        )
        assert action == "open_long"

    def test_skip_on_weak_bull_no_technical(self):
        """Weak bull without technical confirmation = skip (P0.3 fix)."""
        action, _ = _decide_action(sig(), config.NEWS_WEAK_BULL, None)
        assert action == "skip"

    def test_open_long_on_neutral_news_plus_technical(self):
        """Neutral news + technical long = technical-only entry."""
        action, _ = _decide_action(sig(long_entry=True), 0.0, None)
        assert action == "open_long"

    def test_open_short_on_strong_bear_news(self):
        """Strongly bearish news opens short without technical signal.
        Note: _decide_action uses -NEWS_STRONG_BULL as the strong-bear threshold."""
        action, _ = _decide_action(sig(), -config.NEWS_STRONG_BULL, None)
        assert action == "open_short"

    def test_open_short_on_weak_bear_plus_technical(self):
        """Weak bear + technical short = double confirmation.
        Note: weak_bear threshold is -NEWS_WEAK_BULL in _decide_action."""
        action, _ = _decide_action(
            sig(short_entry=True), -config.NEWS_WEAK_BULL, None
        )
        assert action == "open_short"

    def test_skip_on_weak_bear_no_technical(self):
        """Weak bear without technical confirmation = skip (P0.3 fix)."""
        action, _ = _decide_action(sig(), -config.NEWS_WEAK_BULL, None)
        assert action == "skip"

    def test_open_short_on_neutral_news_plus_technical(self):
        """Neutral news + technical short = technical-only entry."""
        action, _ = _decide_action(sig(short_entry=True), 0.0, None)
        assert action == "open_short"

    def test_skip_on_bullish_news_with_short_technical(self):
        """Bullish news + short technical = conflict, skip."""
        action, _ = _decide_action(sig(short_entry=True), 0.20, None)
        assert action == "skip"
