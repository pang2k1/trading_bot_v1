"""test_web_llm.py — tests for /api/llm and /api/compare endpoints.

Uses a temp sqlite fixture for journal.db. All tests run offline.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import journal


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_journal(tmp_path, monkeypatch):
    """Redirect journal.db to a temp file for each test."""
    db = tmp_path / "test_journal.db"
    monkeypatch.setattr(journal, "DB_PATH", db)
    yield db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Create a TestClient with mocked auth and paths."""
    monkeypatch.setenv("WEB_UI_PASSWORD", "test_password_for_ci")
    # Point web_ui at our temp journal.db
    import web_ui
    monkeypatch.setattr(web_ui, "JOURNAL_DB", tmp_path / "test_journal.db")
    # Also point compare.py at the same db
    import compare
    monkeypatch.setattr(compare, "JOURNAL_DB", tmp_path / "test_journal.db")
    # No trades_log.csv
    monkeypatch.setattr(compare, "TRADES_LOG", tmp_path / "nonexistent.csv")

    from fastapi.testclient import TestClient
    with TestClient(web_ui.app) as c:
        yield c


AUTH = ("admin", "test_password_for_ci")


def _insert_decision(action="open_long", confidence=0.8, reasoning="test",
                     model="test-model", executed=False, lessons=None,
                     prompt_tokens=100, completion_tokens=50):
    did = journal.record_decision(
        briefing={}, action=action, confidence=confidence,
        size_multiplier=0.5, reasoning=reasoning,
        invalidation_price=None, model=model,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        executed=executed, lessons_applied=lessons or [],
    )
    return did


def _insert_outcome(did, pnl=1.0):
    journal.record_outcome(
        decision_id=did, entry_price=100.0, exit_price=100.0 + pnl,
        entry_time="2025-01-01T00:00:00+00:00",
        exit_time="2025-01-01T01:00:00+00:00",
        pnl_usd=pnl, exit_reason="signal",
    )


# ── /api/llm ──────────────────────────────────────────────────────────────────

class TestApiLLM:
    def test_empty_journal(self, client):
        r = client.get("/api/llm", auth=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["decisions"] == []
        assert data["aggregates"]["total_decisions"] == 0

    def test_missing_journal(self, client, tmp_path):
        # Point at a path that doesn't exist
        import web_ui
        # tmp_path/test_journal.db already exists from fixture init,
        # but let's point at something that definitely doesn't
        nonexistent = tmp_path / "nope.db"
        # Temporarily override
        orig = web_ui.JOURNAL_DB
        web_ui.JOURNAL_DB = nonexistent
        try:
            r = client.get("/api/llm", auth=AUTH)
            assert r.status_code == 200
            assert r.json()["decisions"] == []
        finally:
            web_ui.JOURNAL_DB = orig

    def test_returns_decisions(self, client):
        _insert_decision(action="open_long", confidence=0.85, reasoning="strong setup")
        _insert_decision(action="hold", confidence=0.3, reasoning="no edge")
        r = client.get("/api/llm", auth=AUTH)
        data = r.json()
        assert len(data["decisions"]) == 2
        # Most recent first
        assert data["decisions"][0]["action"] == "hold"
        assert data["decisions"][1]["action"] == "open_long"

    def test_aggregates(self, client):
        _insert_decision(action="hold", confidence=0.3, reasoning="a", prompt_tokens=100, completion_tokens=10)
        _insert_decision(action="open_long", confidence=0.9, reasoning="b", prompt_tokens=200, completion_tokens=20)
        r = client.get("/api/llm", auth=AUTH)
        agg = r.json()["aggregates"]
        assert agg["total_decisions"] == 2
        assert agg["hold_pct"] == 50.0
        assert agg["avg_confidence"] == 0.6
        assert agg["prompt_tokens"] == 300
        assert agg["completion_tokens"] == 30
        assert agg["estimated_cost_usd"] > 0

    def test_lessons_applied_parsed(self, client):
        _insert_decision(lessons=["L001", "L003"])
        r = client.get("/api/llm", auth=AUTH)
        dec = r.json()["decisions"]
        # Find the decision with lessons
        d = [x for x in dec if x.get("lessons_applied")][0]
        assert "L001" in d["lessons_applied"]

    def test_outcome_pnl_included(self, client):
        did = _insert_decision(action="open_long")
        _insert_outcome(did, pnl=2.5)
        r = client.get("/api/llm", auth=AUTH)
        dec = r.json()["decisions"]
        d = [x for x in dec if x["id"] == did][0]
        assert d["pnl_usd"] == 2.5
        assert d["exit_reason"] == "signal"

    def test_requires_auth(self, client):
        r = client.get("/api/llm")
        assert r.status_code == 401


# ── /api/compare ──────────────────────────────────────────────────────────────

class TestApiCompare:
    def test_empty_data(self, client):
        r = client.get("/api/compare", auth=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["rule"]["total_pnl"] == 0
        assert data["llm"]["total_pnl"] == 0
        assert data["agreement_pct"] == 0

    def test_with_llm_data(self, client):
        did = _insert_decision(action="open_long", confidence=0.8, reasoning="test")
        _insert_outcome(did, pnl=1.5)
        r = client.get("/api/compare", auth=AUTH)
        data = r.json()
        assert data["llm"]["total_pnl"] == 1.5
        assert data["llm"]["trades"] == 1
        assert data["estimated_cost_usd"] >= 0

    def test_buy_and_hold_zero_without_rule_trades(self, client):
        r = client.get("/api/compare", auth=AUTH)
        data = r.json()
        assert data["buy_and_hold"]["pnl"] == 0

    def test_requires_auth(self, client):
        r = client.get("/api/compare")
        assert r.status_code == 401
