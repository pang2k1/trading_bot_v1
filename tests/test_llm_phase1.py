"""test_llm_phase1.py — tests for Phase 1 LLM trader modules.

Covers: validate_decision, journal round-trip, fallback-to-hold on API failure,
briefing assembly. All tests run offline (no network, no exchange).
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

import config
from llm_trader import validate_decision, _hold_decision, _parse_response
import journal


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_journal(tmp_path, monkeypatch):
    """Redirect journal.db to a temp file for each test."""
    db = tmp_path / "test_journal.db"
    monkeypatch.setattr(journal, "DB_PATH", db)
    yield db


def _sample_df(n=50):
    """Build a minimal indicator DataFrame for briefing tests."""
    dates = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(42)
    close = 100 + rng.standard_normal(n).cumsum()
    df = pd.DataFrame({
        "open": close + rng.standard_normal(n) * 0.5,
        "high": close + abs(rng.standard_normal(n)),
        "low": close - abs(rng.standard_normal(n)),
        "close": close,
        "volume": rng.uniform(100, 1000, n),
        "bb_upper": close + 2,
        "bb_mid": close,
        "bb_lower": close - 2,
        "rsi": rng.uniform(30, 70, n),
        "trend1_ema": close,
        "trend1_bias": rng.choice([1, -1], n),
        "trend2_ema": close,
        "trend2_bias": rng.choice([1, -1], n),
    }, index=dates)
    return df


# ── validate_decision tests ────────────────────────────────────────────────────

class TestValidateDecision:
    """Every illegal action/size must be corrected to hold."""

    def test_valid_hold(self):
        d = {"action": "hold", "confidence": 0.3, "size_multiplier": 0.0, "reasoning": "ok"}
        result = validate_decision(d, None)
        assert result["action"] == "hold"

    def test_invalid_action_falls_back(self):
        d = {"action": "buy_all", "confidence": 0.9, "size_multiplier": 1.0, "reasoning": "bad"}
        result = validate_decision(d, None)
        assert result["action"] == "hold"

    def test_open_long_while_long(self):
        d = {"action": "open_long", "confidence": 0.9, "size_multiplier": 1.0, "reasoning": "dup"}
        result = validate_decision(d, "long")
        assert result["action"] == "hold"

    def test_open_short_while_short(self):
        d = {"action": "open_short", "confidence": 0.9, "size_multiplier": 1.0, "reasoning": "dup"}
        result = validate_decision(d, "short")
        assert result["action"] == "hold"

    def test_open_long_while_short(self):
        d = {"action": "open_long", "confidence": 0.9, "size_multiplier": 1.0, "reasoning": "cross"}
        result = validate_decision(d, "short")
        assert result["action"] == "hold"

    def test_open_short_while_long(self):
        d = {"action": "open_short", "confidence": 0.9, "size_multiplier": 1.0, "reasoning": "cross"}
        result = validate_decision(d, "long")
        assert result["action"] == "hold"

    def test_close_with_no_position(self):
        d = {"action": "close", "confidence": 0.8, "size_multiplier": 0, "reasoning": "nothing"}
        result = validate_decision(d, None)
        assert result["action"] == "hold"

    def test_valid_open_long_flat(self):
        d = {"action": "open_long", "confidence": 0.8, "size_multiplier": 0.5, "reasoning": "setup"}
        result = validate_decision(d, None)
        assert result["action"] == "open_long"

    def test_valid_open_short_flat(self):
        d = {"action": "open_short", "confidence": 0.8, "size_multiplier": 0.5, "reasoning": "setup"}
        result = validate_decision(d, None)
        assert result["action"] == "open_short"

    def test_valid_close_long(self):
        d = {"action": "close", "confidence": 0.7, "size_multiplier": 0, "reasoning": "exit"}
        result = validate_decision(d, "long")
        assert result["action"] == "close"

    def test_valid_close_short(self):
        d = {"action": "close", "confidence": 0.7, "size_multiplier": 0, "reasoning": "exit"}
        result = validate_decision(d, "short")
        assert result["action"] == "close"

    def test_low_confidence_entry_rejected(self):
        d = {"action": "open_long", "confidence": 0.3, "size_multiplier": 0.5, "reasoning": "weak"}
        result = validate_decision(d, None)
        assert result["action"] == "hold"

    def test_size_multiplier_clamped(self):
        d = {"action": "hold", "confidence": 0.5, "size_multiplier": 2.0, "reasoning": "big"}
        result = validate_decision(d, None)
        assert result["size_multiplier"] == 1.0

    def test_negative_size_clamped(self):
        d = {"action": "hold", "confidence": 0.5, "size_multiplier": -0.5, "reasoning": "neg"}
        result = validate_decision(d, None)
        assert result["size_multiplier"] == 0.0

    def test_confidence_clamped(self):
        d = {"action": "hold", "confidence": 1.5, "size_multiplier": 0.0, "reasoning": "conf"}
        result = validate_decision(d, None)
        assert result["confidence"] == 1.0


# ── Journal round-trip tests ───────────────────────────────────────────────────

class TestJournal:
    """Write a decision, write an outcome, read it back."""

    def test_record_decision(self, _tmp_journal):
        did = journal.record_decision(
            briefing={"test": True},
            action="open_long",
            confidence=0.85,
            size_multiplier=0.6,
            reasoning="strong setup",
            invalidation_price=95000.0,
            model="deepseek-chat",
            prompt_tokens=1500,
            completion_tokens=200,
        )
        assert did > 0

        decisions = journal.get_recent_decisions(limit=1)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["action"] == "open_long"
        assert d["confidence"] == 0.85
        assert d["model"] == "deepseek-chat"
        assert d["prompt_tokens"] == 1500

    def test_record_outcome(self, _tmp_journal):
        did = journal.record_decision(
            briefing={},
            action="open_long",
            confidence=0.8,
            size_multiplier=0.5,
            reasoning="test",
            invalidation_price=None,
            model="test",
        )
        oid = journal.record_outcome(
            decision_id=did,
            entry_price=100.0,
            exit_price=102.0,
            entry_time="2025-01-01T00:00:00+00:00",
            exit_time="2025-01-01T01:00:00+00:00",
            pnl_usd=1.5,
            exit_reason="signal",
        )
        assert oid > 0

        decisions = journal.get_recent_decisions(limit=1)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["pnl_usd"] == 1.5
        assert d["exit_reason"] == "signal"

    def test_get_decisions_without_outcomes(self, _tmp_journal):
        did = journal.record_decision(
            briefing={}, action="open_long", confidence=0.8,
            size_multiplier=0.5, reasoning="test",
            invalidation_price=None, model="test",
        )
        pending = journal.get_decisions_without_outcomes()
        assert len(pending) == 1
        assert pending[0]["id"] == did

    def test_performance_stats(self, _tmp_journal):
        for pnl in [1.0, -0.5, 2.0, -1.0]:
            did = journal.record_decision(
                briefing={}, action="open_long", confidence=0.8,
                size_multiplier=0.5, reasoning="test",
                invalidation_price=None, model="test",
            )
            journal.record_outcome(
                decision_id=did,
                entry_price=100.0,
                exit_price=100.0 + pnl,
                entry_time="2025-01-01T00:00:00+00:00",
                exit_time="2025-01-01T01:00:00+00:00",
                pnl_usd=pnl,
                exit_reason="test",
            )

        stats = journal.get_performance_stats(days=365)
        assert stats["total_trades"] == 4
        assert stats["win_rate"] == 50.0
        assert stats["total_pnl"] == 1.5

    def test_monthly_token_spend(self, _tmp_journal):
        journal.record_decision(
            briefing={}, action="hold", confidence=0,
            size_multiplier=0, reasoning="",
            invalidation_price=None, model="test",
            prompt_tokens=1000, completion_tokens=100,
        )
        spend = journal.get_monthly_token_spend()
        assert spend["prompt_tokens"] == 1000
        assert spend["completion_tokens"] == 100
        assert spend["total_calls"] == 1


# ── Fallback-to-hold on API failure ───────────────────────────────────────────

class TestFallbackHold:
    """On API error/timeout, the system must fall back to hold."""

    def test_api_timeout_returns_hold(self):
        """Mock the LLM client to raise, verify make_decision returns hold."""
        import llm_trader

        with patch("llm_trader.llm_client.complete", side_effect=TimeoutError("API timeout")):
            df = _sample_df()
            result = llm_trader.make_decision(
                df=df, symbol="BTC/USDT", balance=100.0,
                state=None, news_cache={"BTC/USDT": 0.1},
            )
        assert result["action"] == "hold"
        assert "API error" in result["reasoning"]

    def test_api_error_returns_hold(self):
        """Mock the LLM client to raise a generic error."""
        import llm_trader

        with patch("llm_trader.llm_client.complete", side_effect=ConnectionError("no network")):
            df = _sample_df()
            result = llm_trader.make_decision(
                df=df, symbol="BTC/USDT", balance=100.0,
            )
        assert result["action"] == "hold"

    def test_circuit_breaker_skips_call(self):
        """When circuit breaker is halted, no LLM call should be made."""
        import llm_trader

        with patch("llm_trader.llm_client.complete") as mock_api:
            df = _sample_df()
            result = llm_trader.make_decision(
                df=df, symbol="BTC/USDT", balance=100.0,
                circuit_halted=True,
            )
        mock_api.assert_not_called()
        assert result["action"] == "hold"
        assert "Circuit breaker" in result["reasoning"]


# ── Briefing assembly tests ────────────────────────────────────────────────────

class TestBriefing:
    """Test that the briefing is assembled correctly and stays under ~3000 tokens."""

    def test_build_technical(self):
        from briefing import build_technical
        df = _sample_df()
        tech = build_technical(df)
        assert "bb_position" in tech
        assert "rsi" in tech
        assert 0 <= tech["bb_position"] <= 1
        assert 0 <= tech["rsi"] <= 100
        assert tech["price"] > 0

    def test_build_account_no_position(self):
        from briefing import build_account
        acct = build_account(balance=100.0)
        assert acct["balance_usdt"] == 100.0
        assert acct["open_position"] is None

    def test_build_account_with_position(self):
        from briefing import build_account
        state = {"BTC/USDT": {
            "side": "long", "entry_price": 100000, "qty": 0.001,
            "stop_loss": 99000, "entry_time": "2025-01-01T00:00:00Z",
            "notional": 100,
        }}
        acct = build_account(balance=100.0, state=state)
        assert acct["open_position"]["side"] == "long"
        assert acct["open_position"]["entry_price"] == 100000

    def test_assemble_briefing(self):
        from briefing import assemble_briefing, briefing_to_text
        df = _sample_df()
        briefing = assemble_briefing(
            df=df, symbol="BTC/USDT", balance=100.0,
            state=None, news_cache={"BTC/USDT": 0.1},
        )
        assert briefing["symbol"] == "BTC/USDT"
        assert "technical" in briefing
        assert "rule_engine_signals" in briefing
        assert "news" in briefing
        assert "account" in briefing
        assert "memory" in briefing

        text = briefing_to_text(briefing)
        # Rough token estimate: ~4 chars per token
        assert len(text) / 4 < 3000, f"Briefing too long: ~{len(text)//4} tokens"

    def test_briefing_under_3000_tokens(self):
        from briefing import assemble_briefing, briefing_to_text
        df = _sample_df()
        state = {"BTC/USDT": {
            "side": "long", "entry_price": 100000, "qty": 0.001,
            "stop_loss": 99000, "entry_time": "2025-01-01T00:00:00Z",
            "notional": 100,
        }}
        briefing = assemble_briefing(
            df=df, symbol="BTC/USDT", balance=100.0,
            state=state, news_cache={"BTC/USDT": 0.1},
        )
        text = briefing_to_text(briefing)
        token_estimate = len(text) / 4
        assert token_estimate < 3000, f"Briefing ~{token_estimate:.0f} tokens — must stay under 3000"


# ── Response parsing tests ────────────────────────────────────────────────────

class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_tool_call(self):
        response = {
            "tool_calls": [{
                "id": "call_123",
                "name": "submit_decision",
                "arguments": json.dumps({
                    "action": "open_long",
                    "confidence": 0.85,
                    "size_multiplier": 0.6,
                    "reasoning": "strong bull setup",
                }),
            }],
            "content": "",
        }
        result = _parse_response(response)
        assert result["action"] == "open_long"
        assert result["confidence"] == 0.85

    def test_parse_json_content_fallback(self):
        response = {
            "tool_calls": [],
            "content": json.dumps({
                "action": "hold",
                "confidence": 0.2,
                "size_multiplier": 0.0,
                "reasoning": "uncertain",
            }),
        }
        result = _parse_response(response)
        assert result["action"] == "hold"

    def test_parse_empty_raises(self):
        response = {"tool_calls": [], "content": ""}
        with pytest.raises(ValueError, match="No tool calls"):
            _parse_response(response)
