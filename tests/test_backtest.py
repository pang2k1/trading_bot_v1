"""test_backtest.py — hand-computed round-trip tests for long and short positions."""

import numpy as np
import pandas as pd
import pytest

import config
import backtest


def _make_signal_df(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame suitable for backtest.run() from a list of row dicts."""
    defaults = {
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 500.0,
        "long_entry": False, "long_exit": False,
        "short_entry": False, "short_exit": False,
    }
    records = []
    for i, row in enumerate(rows):
        r = {**defaults, **row}
        records.append(r)
    idx = pd.date_range("2025-01-01", periods=len(records), freq="15min", tz="UTC")
    return pd.DataFrame(records, index=idx)


class TestLongRoundTrip:
    def test_single_long_profit(self):
        """Open long at 100, close at 110. Expect PnL after commissions + slippage."""
        config.INITIAL_CAPITAL = 1000.0
        config.RISK_PER_TRADE = 0.20
        config.COMMISSION = 0.001
        config.SLIPPAGE_PCT = 0.0
        config.STOP_LOSS_PCT = 0.05
        config.LONG_ONLY = True

        df = _make_signal_df([
            {"close": 100.0, "long_entry": True},   # entry
            {"close": 110.0, "long_exit": True},     # exit
        ])
        metrics, trades = backtest.run(df)

        # Notional = 1000 * 0.20 = 200, qty = 200/100 = 2
        # entry_comm = 200 * 0.001 = 0.20
        # exit_comm = 2 * 110 * 0.001 = 0.22
        # proceeds = 2*110 - 0.22 = 219.78
        # pnl = 219.78 - 200 - 0.20 = 19.58
        # final equity = 1000 - 200 - 0.20 + 200 + 0.20 + 19.58 = 1019.58
        assert len(trades) == 1
        assert trades.iloc[0]["side"] == "long"
        assert abs(trades.iloc[0]["pnl_usd"] - 19.58) < 0.01
        assert abs(metrics["final_equity"] - (1000.0 + 19.58)) < 0.01

    def test_single_long_loss(self):
        """Open long at 100, close at 95. Expect loss."""
        config.INITIAL_CAPITAL = 1000.0
        config.RISK_PER_TRADE = 0.20
        config.COMMISSION = 0.001
        config.SLIPPAGE_PCT = 0.0
        config.STOP_LOSS_PCT = 0.05
        config.LONG_ONLY = True

        df = _make_signal_df([
            {"close": 100.0, "long_entry": True},
            {"close": 95.0, "long_exit": True},
        ])
        metrics, trades = backtest.run(df)

        # qty = 2, entry_comm = 0.20, exit_comm = 2*95*0.001 = 0.19
        # proceeds = 2*95 - 0.19 = 189.81
        # pnl = 189.81 - 200 - 0.20 = -10.39
        assert len(trades) == 1
        assert trades.iloc[0]["pnl_usd"] < 0
        assert abs(metrics["final_equity"] - (1000.0 - 10.39)) < 0.01


class TestShortRoundTrip:
    def test_short_no_phantom_equity(self):
        """Short opened and closed at the SAME price should lose exactly entry_comm + exit_comm.
        Equity should never jump by notional."""
        config.INITIAL_CAPITAL = 1000.0
        config.RISK_PER_TRADE = 0.20
        config.COMMISSION = 0.001
        config.SLIPPAGE_PCT = 0.0
        config.STOP_LOSS_PCT = 0.05
        config.LONG_ONLY = False

        price = 100.0
        df = _make_signal_df([
            {"close": price, "short_entry": True},
            {"close": price, "short_exit": True},
        ])
        metrics, trades = backtest.run(df)

        # notional = 200, qty = 2
        # entry_comm = 0.20, exit_comm = 2*100*0.001 = 0.20
        # pnl = (100-100)*2 - 0.20 - 0.20 = -0.40
        expected_pnl = -(0.20 + 0.20)
        assert len(trades) == 1
        assert trades.iloc[0]["side"] == "short"
        assert abs(trades.iloc[0]["pnl_usd"] - expected_pnl) < 0.001
        # Final equity should be initial - 0.40, NOT initial + 200 - 0.40
        assert abs(metrics["final_equity"] - (1000.0 + expected_pnl)) < 0.001

    def test_short_profit(self):
        """Short at 100, close at 90. Should profit."""
        config.INITIAL_CAPITAL = 1000.0
        config.RISK_PER_TRADE = 0.20
        config.COMMISSION = 0.001
        config.SLIPPAGE_PCT = 0.0
        config.STOP_LOSS_PCT = 0.05
        config.LONG_ONLY = False

        df = _make_signal_df([
            {"close": 100.0, "short_entry": True},
            {"close": 90.0, "short_exit": True},
        ])
        metrics, trades = backtest.run(df)

        # qty=2, entry_comm=0.20, exit_comm=2*90*0.001=0.18
        # pnl = (100-90)*2 - 0.18 - 0.20 = 19.62
        # Open: equity -= 0.20 → 999.80
        # Close: equity += 19.62 + 0.20 = 1019.62
        expected_pnl = 2 * 10 - 0.18 - 0.20
        assert len(trades) == 1
        assert abs(trades.iloc[0]["pnl_usd"] - expected_pnl) < 0.01
        assert abs(metrics["final_equity"] - (1000.0 + expected_pnl)) < 0.01


class TestStopLoss:
    def test_stop_loss_long(self):
        """Stop-loss should trigger when bar low drops below stop level."""
        config.INITIAL_CAPITAL = 1000.0
        config.RISK_PER_TRADE = 0.20
        config.COMMISSION = 0.001
        config.SLIPPAGE_PCT = 0.0
        config.STOP_LOSS_PCT = 0.05
        config.LONG_ONLY = True

        # Entry at 100, stop at 95. Next bar low at 90 → stop triggered at 95.
        df = _make_signal_df([
            {"close": 100.0, "long_entry": True},
            {"close": 90.0, "low": 90.0},  # low below stop triggers SL
        ])
        metrics, trades = backtest.run(df)

        assert len(trades) == 1
        assert "stop-loss" in trades.iloc[0].get("note", "")
        # exit price should be the stop price (95), not close (90)
        assert abs(trades.iloc[0]["exit_price"] - 95.0) < 0.01
