"""test_dust_positions.py — tests for dust-handling in _sync_positions_from_exchange and _close_long/_close_short."""

from unittest.mock import MagicMock, patch

import pytest

import config
from live_trader import (
    _close_long,
    _close_short,
    _is_closeable,
    _sync_positions_from_exchange,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_exchange(markets=None):
    """Build a mock ccxt.binance with configurable market limits."""
    exchange = MagicMock()
    exchange.markets = markets or {}
    return exchange


def _btc_market(min_amount=0.00001, min_cost=5.0):
    return {
        "BTC/USDT": {
            "limits": {
                "amount": {"min": min_amount},
                "cost":   {"min": min_cost},
            },
        },
    }


def _pos_long(qty=0.001, entry_price=60000.0):
    return {
        "side":        "long",
        "entry_price": entry_price,
        "qty":         qty,
        "stop_loss":   round(entry_price * (1 - config.STOP_LOSS_PCT), 8),
        "entry_time":  "2026-06-12T00:00:00+00:00",
        "sl_order_id": None,
    }


def _pos_short(qty=0.001, entry_price=60000.0):
    return {
        "side":        "short",
        "entry_price": entry_price,
        "qty":         qty,
        "stop_loss":   round(entry_price * (1 + config.STOP_LOSS_PCT), 8),
        "entry_time":  "2026-06-12T00:00:00+00:00",
        "sl_order_id": None,
    }


# ── _is_closeable ─────────────────────────────────────────────────────────────

class TestIsCloseable:
    def test_above_both_limits(self):
        exchange = _make_exchange(_btc_market())
        assert _is_closeable(exchange, "BTC/USDT", 0.001, 60000.0) is True

    def test_below_min_amount(self):
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        assert _is_closeable(exchange, "BTC/USDT", 0.0000073, 60000.0) is False

    def test_below_min_cost(self):
        exchange = _make_exchange(_btc_market(min_cost=5.0))
        assert _is_closeable(exchange, "BTC/USDT", 0.001, 1.0) is False  # notional = 0.001

    def test_unknown_market_passes(self):
        exchange = _make_exchange({})
        assert _is_closeable(exchange, "BTC/USDT", 0.0000073, 60000.0) is True

    def test_zero_limits_pass(self):
        exchange = _make_exchange({"BTC/USDT": {"limits": {"amount": {"min": 0}, "cost": {"min": 0}}}})
        assert _is_closeable(exchange, "BTC/USDT", 0.0000073, 60000.0) is True


# ── _close_long dust path ─────────────────────────────────────────────────────

class TestCloseLongDust:
    def test_dust_long_drops_without_order(self):
        """Dust qty below min_amount → no sell order placed, state entry removed."""
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        state = {"BTC/USDT": _pos_long(qty=0.0000073)}
        _close_long(exchange, "BTC/USDT", state)
        assert "BTC/USDT" not in state
        exchange.create_market_sell_order.assert_not_called()

    def test_normal_long_places_order(self):
        """Normal qty → sell order placed as usual."""
        exchange = _make_exchange(_btc_market())
        exchange.create_market_sell_order.return_value = {
            "average": 61000.0, "price": 61000.0, "id": "123", "fees": [],
        }
        state = {"BTC/USDT": _pos_long(qty=0.001)}
        with patch("live_trader._log_trade"), patch("live_trader._record_llm_outcome"):
            _close_long(exchange, "BTC/USDT", state)
        exchange.create_market_sell_order.assert_called_once()
        assert "BTC/USDT" not in state


# ── _close_short dust path ────────────────────────────────────────────────────

class TestCloseShortDust:
    def test_dust_short_drops_without_order(self):
        """Dust qty below min_amount → no buy order placed, state entry removed."""
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        state = {"BTC/USDT": _pos_short(qty=0.0000073)}
        _close_short(exchange, "BTC/USDT", state)
        assert "BTC/USDT" not in state
        exchange.create_market_buy_order.assert_not_called()

    def test_normal_short_places_order(self):
        """Normal qty → buy order placed as usual."""
        exchange = _make_exchange(_btc_market())
        exchange.create_market_buy_order.return_value = {
            "average": 59000.0, "price": 59000.0, "id": "456", "fees": [],
        }
        state = {"BTC/USDT": _pos_short(qty=0.001)}
        with patch("live_trader._log_trade"), patch("live_trader._record_llm_outcome"):
            _close_short(exchange, "BTC/USDT", state)
        exchange.create_market_buy_order.assert_called_once()
        assert "BTC/USDT" not in state


# ── _sync_positions_from_exchange dust path ────────────────────────────────────

class TestSyncDustOrphan:
    @patch("live_trader._save_state")
    def test_dust_long_orphan_not_adopted(self, mock_save):
        """Orphaned base asset below exchange min → ignored, not adopted."""
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        exchange.fetch_balance.return_value = {
            "BTC": {"total": 0.0000073, "debt": 0, "borrowed": 0, "free": 0},
        }
        exchange.fetch_ticker.return_value = {"last": 60000.0}
        state = {}
        _sync_positions_from_exchange(exchange, ["BTC/USDT"], state)
        assert "BTC/USDT" not in state

    @patch("live_trader._save_state")
    def test_dust_short_orphan_not_adopted(self, mock_save):
        """Orphaned base debt below exchange min → ignored, not adopted."""
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        exchange.fetch_balance.return_value = {
            "BTC": {"total": 0, "debt": 0.0000073, "borrowed": 0.0000073, "free": 0},
        }
        exchange.fetch_ticker.return_value = {"last": 60000.0}
        state = {}
        _sync_positions_from_exchange(exchange, ["BTC/USDT"], state)
        assert "BTC/USDT" not in state

    @patch("live_trader._save_state")
    def test_valid_orphan_long_adopted(self, mock_save):
        """Orphaned base asset above exchange min → adopted."""
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        exchange.fetch_balance.return_value = {
            "BTC": {"total": 0.001, "debt": 0, "borrowed": 0, "free": 0},
        }
        exchange.fetch_ticker.return_value = {"last": 60000.0}
        state = {}
        _sync_positions_from_exchange(exchange, ["BTC/USDT"], state)
        assert "BTC/USDT" in state
        assert state["BTC/USDT"]["side"] == "long"
        assert state["BTC/USDT"]["adopted"] is True

    @patch("live_trader._save_state")
    def test_valid_orphan_short_adopted(self, mock_save):
        """Orphaned base debt above exchange min → adopted."""
        exchange = _make_exchange(_btc_market(min_amount=0.00001))
        exchange.fetch_balance.return_value = {
            "BTC": {"total": 0, "debt": 0.001, "borrowed": 0.001, "free": 0},
        }
        exchange.fetch_ticker.return_value = {"last": 60000.0}
        state = {}
        _sync_positions_from_exchange(exchange, ["BTC/USDT"], state)
        assert "BTC/USDT" in state
        assert state["BTC/USDT"]["side"] == "short"
        assert state["BTC/USDT"]["adopted"] is True
