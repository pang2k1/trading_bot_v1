"""test_strategy.py — synthetic OHLCV tests for boolean signal columns."""

import numpy as np
import pandas as pd
import pytest

import config
import strategy


def _make_base_df(n=100, close=None, rsi=None, bb_lower=None, bb_mid=None,
                  bb_upper=None, trend1_bias=1, trend2_bias=1):
    """Build a minimal DataFrame with indicator columns already present."""
    idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(42)
    df = pd.DataFrame({
        "open":   rng.normal(100, 1, n),
        "high":   rng.normal(101, 1, n),
        "low":    rng.normal(99, 1, n),
        "close":  close if close is not None else rng.normal(100, 1, n),
        "volume": rng.uniform(100, 1000, n),
    }, index=idx)
    df["bb_lower"] = bb_lower if bb_lower is not None else 98.0
    df["bb_mid"]   = bb_mid   if bb_mid   is not None else 100.0
    df["bb_upper"] = bb_upper if bb_upper is not None else 102.0
    df["rsi"] = rsi if rsi is not None else 50.0
    df["trend1_bias"] = trend1_bias
    df["trend2_bias"] = trend2_bias
    return df


class TestLongEntry:
    def test_fires_when_all_conditions_met(self):
        """Long entry fires when close <= bb_lower, rsi < threshold, and both biases bullish."""
        df = _make_base_df(
            n=10,
            close=np.linspace(99, 97, 10),  # dropping prices
            rsi=30.0,  # below RSI_LONG_ENTRY
            bb_lower=98.0,
            trend1_bias=1,
            trend2_bias=1,
        )
        # Bar where close <= bb_lower should trigger long_entry
        result = strategy.generate_signals(df)
        entries = result["long_entry"]
        assert entries.any(), "Expected at least one long entry signal"
        # All entry bars should have close <= bb_lower
        for i in entries[entries].index:
            assert result.loc[i, "close"] <= result.loc[i, "bb_lower"]

    def test_no_entry_without_bullish_trend(self):
        """No long entry when trend bias is bearish, even if price/RSI qualify."""
        df = _make_base_df(
            close=97.0,
            rsi=30.0,
            bb_lower=98.0,
            trend1_bias=-1,  # bearish
            trend2_bias=-1,
        )
        result = strategy.generate_signals(df)
        assert not result["long_entry"].any()


class TestShortEntry:
    def test_fires_when_all_conditions_met(self):
        df = _make_base_df(
            close=103.0,
            rsi=70.0,
            bb_upper=102.0,
            trend1_bias=-1,
            trend2_bias=-1,
        )
        result = strategy.generate_signals(df)
        assert result["short_entry"].any()

    def test_no_entry_without_bearish_trend(self):
        df = _make_base_df(
            close=103.0,
            rsi=70.0,
            bb_upper=102.0,
            trend1_bias=1,
            trend2_bias=1,
        )
        result = strategy.generate_signals(df)
        assert not result["short_entry"].any()


class TestNoCollision:
    def test_entry_not_masked_by_exit(self):
        """A bar that triggers both long_entry and short_exit should set both booleans True.
        Under the old int-column scheme, the exit would overwrite the entry."""
        n = 5
        df = _make_base_df(
            n=n,
            close=96.0,       # <= bb_lower (long entry) AND <= bb_mid (short exit)
            rsi=30.0,         # < RSI_LONG_ENTRY
            bb_lower=98.0,
            bb_mid=100.0,
            bb_upper=102.0,
            trend1_bias=1,    # bullish for long entry
            trend2_bias=1,
        )
        result = strategy.generate_signals(df)
        # long_entry should be True (close <= bb_lower, rsi < threshold, bullish)
        assert result["long_entry"].all(), "Long entry should fire on every bar"
        # short_exit should also be True (close <= bb_mid)
        assert result["short_exit"].all(), "Short exit should fire on every bar"

    def test_short_entry_not_masked_by_long_exit(self):
        """A bar that triggers short_entry and long_exit should have both True."""
        df = _make_base_df(
            close=103.0,       # >= bb_upper (short entry) AND >= bb_mid (long exit)
            rsi=70.0,          # > RSI_SHORT_ENTRY
            bb_lower=98.0,
            bb_mid=100.0,
            bb_upper=102.0,
            trend1_bias=-1,    # bearish for short entry
            trend2_bias=-1,
        )
        result = strategy.generate_signals(df)
        assert result["short_entry"].all(), "Short entry should fire on every bar"
        assert result["long_exit"].all(), "Long exit should fire on every bar (close >= bb_mid)"


class TestExits:
    def test_long_exit_on_bb_mid_touch(self):
        df = _make_base_df(close=101.0, bb_mid=100.0)
        result = strategy.generate_signals(df)
        assert result["long_exit"].any()

    def test_short_exit_on_bb_mid_touch(self):
        df = _make_base_df(close=99.0, bb_mid=100.0)
        result = strategy.generate_signals(df)
        assert result["short_exit"].any()

    def test_long_exit_on_rsi_high(self):
        df = _make_base_df(close=100.0, rsi=80.0, bb_mid=101.0)
        result = strategy.generate_signals(df)
        assert result["long_exit"].any()

    def test_short_exit_on_rsi_low(self):
        df = _make_base_df(close=100.0, rsi=20.0, bb_mid=99.0)
        result = strategy.generate_signals(df)
        assert result["short_exit"].any()

    def test_long_exit_on_trend_flip(self):
        df = _make_base_df(close=100.0, rsi=50.0, bb_mid=101.0, trend1_bias=-1)
        result = strategy.generate_signals(df)
        assert result["long_exit"].any()

    def test_short_exit_on_trend_flip(self):
        df = _make_base_df(close=100.0, rsi=50.0, bb_mid=99.0, trend1_bias=1)
        result = strategy.generate_signals(df)
        assert result["short_exit"].any()
