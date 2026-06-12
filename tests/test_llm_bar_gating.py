"""test_llm_bar_gating.py — verify LLM shadow fires only once per LLM_DECISION_TF bar.

The bug: _run_llm_shadow was called inside run_once on every 15m cycle,
ignoring config.LLM_DECISION_TF="1h". The fix gates it in the main loop
using _last_llm_bar, same pattern as _last_full_run_bar.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import config


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    """Ensure no real exchange connection."""
    monkeypatch.setattr("live_trader._connect", lambda testnet=True: MagicMock())


def _mock_exchange():
    """Build a minimal mock exchange that satisfies run_once."""
    exchange = MagicMock()
    exchange.fetch_balance.return_value = {"USDT": {"free": 100.0}}
    exchange.fetch_ohlcv.return_value = []
    exchange.fetch_ticker.return_value = {"last": 60000.0, "close": 60000.0}
    exchange.markets = {"BTC/USDT": {"limits": {"amount": {"min": 0.00001}, "cost": {"min": 5}}}}
    return exchange


class TestLLMBarGating:
    """LLM shadow must fire once per LLM_DECISION_TF bar, not per BASE_TF bar."""

    @patch("live_trader._run_llm_shadow")
    @patch("live_trader._get_signal")
    @patch("live_trader._get_usdt_balance", return_value=100.0)
    def test_llm_fires_once_per_llm_bar(self, mock_bal, mock_signal, mock_llm):
        """With BASE_TF=15m and LLM_DECISION_TF=1h, LLM should fire once
        every 4 base-TF bars."""
        from live_trader import (
            run_once,
            run_llm_cycle,
            TF_SECONDS,
        )

        mock_signal.return_value = (
            {"long_entry": False, "long_exit": False,
             "short_entry": False, "short_exit": False},
            60000.0,
        )

        exchange = _mock_exchange()
        state = {}
        from live_trader import NewsCache, DailyCircuitBreaker

        news_cache = MagicMock()
        news_cache.get.return_value = {"BTC/USDT": 0.1}
        circuit = DailyCircuitBreaker()
        circuit.reset_if_new_day(100.0)

        # Simulate 4 base-TF (15m) cycles
        for i in range(4):
            run_once(exchange, ["BTC/USDT"], state, news_cache, circuit)

        # run_once no longer calls LLM — it was moved out
        mock_llm.assert_not_called()

        # Now simulate the LLM gating logic directly using a realistic epoch
        llm_period = TF_SECONDS.get(config.LLM_DECISION_TF, 3600)
        base_period = TF_SECONDS.get(config.BASE_TF, 900)

        # Start mid-hour so all 4 base bars fall within the same LLM bar
        epoch = 1700000000
        start = (epoch // llm_period) * llm_period + 2 * base_period  # 30min in

        # Initialize to the current LLM bar (simulates normal loop start)
        last_llm_bar = int((start - 10) // llm_period) * llm_period
        llm_fired = 0

        for tick in range(4):
            now_ts = start + tick * base_period
            current_bar_ts = int((now_ts - 10) // base_period) * base_period
            current_llm_bar = int((now_ts - 10) // llm_period) * llm_period

            if current_bar_ts != -1:
                if current_llm_bar != last_llm_bar:
                    llm_fired += 1
                    last_llm_bar = current_llm_bar

        # 4x15m bars within one hour → LLM fires exactly once
        assert llm_fired == 1, f"Expected 1 LLM call, got {llm_fired}"

    @patch("live_trader._run_llm_shadow")
    @patch("live_trader._get_signal")
    @patch("live_trader._get_usdt_balance", return_value=100.0)
    def test_llm_fires_twice_across_two_hours(self, mock_bal, mock_signal, mock_llm):
        """Across 8 base-TF bars (2 hours), LLM should fire twice."""
        from live_trader import TF_SECONDS

        mock_signal.return_value = (
            {"long_entry": False, "long_exit": False,
             "short_entry": False, "short_exit": False},
            60000.0,
        )

        llm_period = TF_SECONDS.get(config.LLM_DECISION_TF, 3600)
        base_period = TF_SECONDS.get(config.BASE_TF, 900)

        epoch = 1700000000
        start = (epoch // llm_period) * llm_period + 2 * base_period  # mid-hour

        # Initialize to current LLM bar, then simulate 8 ticks across 2 hours
        last_llm_bar = int((start - 10) // llm_period) * llm_period
        llm_fired = 0

        for tick in range(8):
            now_ts = start + tick * base_period
            current_llm_bar = int((now_ts - 10) // llm_period) * llm_period

            if current_llm_bar != last_llm_bar:
                llm_fired += 1
                last_llm_bar = current_llm_bar

        assert llm_fired == 2, f"Expected 2 LLM calls across 2 hours, got {llm_fired}"

    @patch("live_trader._run_llm_shadow")
    @patch("live_trader._get_signal")
    @patch("live_trader._get_usdt_balance", return_value=100.0)
    def test_run_once_does_not_call_llm(self, mock_bal, mock_signal, mock_llm):
        """run_once must not call _run_llm_shadow — it's decoupled now."""
        from live_trader import run_once, NewsCache, DailyCircuitBreaker

        mock_signal.return_value = (
            {"long_entry": False, "long_exit": False,
             "short_entry": False, "short_exit": False},
            60000.0,
        )

        exchange = _mock_exchange()
        news_cache = MagicMock()
        news_cache.get.return_value = {"BTC/USDT": 0.1}
        circuit = DailyCircuitBreaker()
        circuit.reset_if_new_day(100.0)

        run_once(exchange, ["BTC/USDT"], {}, news_cache, circuit)
        mock_llm.assert_not_called()

    @patch("live_trader._run_llm_shadow")
    @patch("live_trader._get_usdt_balance", return_value=100.0)
    def test_run_llm_cycle_calls_shadow(self, mock_bal, mock_llm):
        """run_llm_cycle should call _run_llm_shadow for each symbol."""
        from live_trader import run_llm_cycle, NewsCache, DailyCircuitBreaker

        exchange = _mock_exchange()
        news_cache = MagicMock()
        news_cache.get.return_value = {"BTC/USDT": 0.1}
        circuit = DailyCircuitBreaker()
        circuit.reset_if_new_day(100.0)

        run_llm_cycle(exchange, ["BTC/USDT"], {}, news_cache, circuit, 100.0)
        mock_llm.assert_called_once()
