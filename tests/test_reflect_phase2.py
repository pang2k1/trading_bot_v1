"""test_reflect_phase2.py — tests for Phase 2 reflection / playbook modules.

Covers: playbook parsing, edit validation, edit application, journal queries,
lesson decay, reflection flow (mocked LLM), response parsing.
All tests run offline (no network, no exchange).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config
import journal
from playbook import (
    Lesson,
    apply_edits,
    count_words,
    format_lesson_block,
    load_lessons,
    next_lesson_id,
    save_lessons,
    validate_edit,
)
from reflect import (
    apply_decay,
    build_reflection_prompt,
    parse_reflection_response,
    run_reflection,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_journal(tmp_path, monkeypatch):
    """Redirect journal.db to a temp file for each test."""
    db = tmp_path / "test_journal.db"
    monkeypatch.setattr(journal, "DB_PATH", db)
    yield db


@pytest.fixture
def tmp_playbook(tmp_path):
    return tmp_path / "test_playbook.md"


def _seed_lessons():
    return [
        Lesson(id="L001", text="Avoid flat RSI entries.", created="2025-01-01",
               evidence=["none"], hits=0),
        Lesson(id="L002", text="Respect strong news contrary to technicals.", created="2025-01-01",
               evidence=["D100", "D101", "D102"], hits=5),
        Lesson(id="L003", text="Reduce size after consecutive losses.", created="2025-01-01",
               evidence=["D200", "D201", "D202"], hits=3),
    ]


def _insert_trade_with_outcome(action="open_long", reasoning="test", pnl=1.0,
                                days_ago=0, lessons_applied=None):
    """Insert a decision + outcome with a controlled timestamp."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    did = journal.record_decision(
        briefing={}, action=action, confidence=0.8,
        size_multiplier=0.5, reasoning=reasoning,
        invalidation_price=None, model="test",
        lessons_applied=lessons_applied or [],
    )
    # Override timestamp to the desired day
    conn = journal._connect()
    try:
        conn.execute("UPDATE decisions SET timestamp = ? WHERE id = ?", (ts, did))
        conn.commit()
    finally:
        conn.close()

    journal.record_outcome(
        decision_id=did, entry_price=100.0, exit_price=100.0 + pnl,
        entry_time="2025-01-01T00:00:00+00:00",
        exit_time="2025-01-01T01:00:00+00:00",
        pnl_usd=pnl, exit_reason="signal",
    )
    return did


# ── Playbook parsing ──────────────────────────────────────────────────────────

class TestPlaybookParser:
    def test_parse_missing_file(self, tmp_path):
        lessons = load_lessons(tmp_path / "nonexistent.md")
        assert lessons == []

    def test_parse_empty_file(self, tmp_playbook):
        tmp_playbook.write_text("", encoding="utf-8")
        assert load_lessons(tmp_playbook) == []

    def test_parse_seed_playbook(self):
        path = Path("playbook.example.md")
        if not path.exists():
            pytest.skip("playbook.example.md not found")
        lessons = load_lessons(path)
        assert len(lessons) == 3
        assert lessons[0].id == "L001"
        assert "RSI" in lessons[0].text
        assert lessons[1].id == "L002"

    def test_round_trip(self, tmp_playbook):
        lessons = _seed_lessons()
        save_lessons(lessons, tmp_playbook)
        loaded = load_lessons(tmp_playbook)
        assert len(loaded) == 3
        assert loaded[0].id == "L001"
        assert loaded[0].text == "Avoid flat RSI entries."
        assert loaded[1].hits == 5

    def test_next_lesson_id(self):
        lessons = [Lesson(id="L001", text="a", created="2025-01-01")]
        assert next_lesson_id(lessons) == "L002"
        lessons.append(Lesson(id="L005", text="b", created="2025-01-01"))
        assert next_lesson_id(lessons) == "L006"

    def test_count_words(self):
        assert count_words("hello world") == 2
        assert count_words("") == 0
        assert count_words("a b c d e") == 5

    def test_format_lesson_block(self):
        lesson = Lesson(id="L010", text="Test rule.", created="2025-06-01",
                        evidence=["D1", "D2", "D3"], hits=4)
        block = format_lesson_block(lesson)
        assert "<!-- lesson: L010 -->" in block
        assert "<!-- /lesson: L010 -->" in block
        assert "**Rule:** Test rule." in block
        assert "**Hits:** 4" in block
        assert "**Evidence:** D1, D2, D3" in block


# ── Edit validation ───────────────────────────────────────────────────────────

class TestPlaybookValidation:
    def test_reject_invalid_op(self):
        edit = {"op": "delete", "lesson_id": "L001", "text": "t", "evidence": ["D1", "D2", "D3"]}
        assert validate_edit(edit, _seed_lessons()) is None

    def test_reject_fewer_than_3_evidence(self):
        edit = {"op": "add", "lesson_id": "L004", "text": "Rule", "evidence": ["D1", "D2"]}
        assert validate_edit(edit, _seed_lessons()) is None

    def test_reject_too_many_words(self):
        long_text = " ".join(["word"] * 200)
        edit = {"op": "add", "lesson_id": "L004", "text": long_text,
                "evidence": ["D1", "D2", "D3"]}
        assert validate_edit(edit, _seed_lessons()) is None

    def test_reject_add_at_cap(self, monkeypatch):
        monkeypatch.setattr(config, "PLAYBOOK_MAX_LESSONS", 3)
        edit = {"op": "add", "lesson_id": "L004", "text": "New rule",
                "evidence": ["D1", "D2", "D3"]}
        assert validate_edit(edit, _seed_lessons()) is None

    def test_reject_update_nonexistent(self):
        edit = {"op": "update", "lesson_id": "L999", "text": "Updated",
                "evidence": ["D1", "D2", "D3"]}
        assert validate_edit(edit, _seed_lessons()) is None

    def test_reject_remove_nonexistent(self):
        edit = {"op": "remove", "lesson_id": "L999", "text": "", "evidence": []}
        assert validate_edit(edit, _seed_lessons()) is None

    def test_accept_valid_add(self):
        edit = {"op": "add", "lesson_id": "L004", "text": "New rule",
                "evidence": ["D1", "D2", "D3"]}
        assert validate_edit(edit, _seed_lessons()) is not None

    def test_accept_valid_update(self):
        edit = {"op": "update", "lesson_id": "L002", "text": "Better rule",
                "evidence": ["D100", "D101", "D102", "D103"]}
        assert validate_edit(edit, _seed_lessons()) is not None

    def test_accept_remove_without_evidence(self):
        edit = {"op": "remove", "lesson_id": "L002", "text": "", "evidence": []}
        assert validate_edit(edit, _seed_lessons()) is not None


# ── Edit application ──────────────────────────────────────────────────────────

class TestPlaybookEdits:
    def test_apply_add_creates_lesson(self, tmp_playbook):
        lessons = _seed_lessons()
        edits = [{"op": "add", "lesson_id": "L004", "text": "New lesson",
                  "evidence": ["D1", "D2", "D3"]}]
        new, applied, rejected = apply_edits(lessons, edits)
        assert len(applied) == 1
        assert len(new) == 4
        assert new[-1].id == "L004"

    def test_apply_update_modifies_text(self, tmp_playbook):
        lessons = _seed_lessons()
        edits = [{"op": "update", "lesson_id": "L002",
                  "text": "Updated rule", "evidence": ["D100", "D101", "D102"]}]
        new, applied, rejected = apply_edits(lessons, edits)
        assert len(applied) == 1
        updated = [l for l in new if l.id == "L002"][0]
        assert updated.text == "Updated rule"

    def test_apply_remove_deletes_lesson(self):
        lessons = _seed_lessons()
        edits = [{"op": "remove", "lesson_id": "L002", "text": "", "evidence": []}]
        new, applied, rejected = apply_edits(lessons, edits)
        assert len(applied) == 1
        assert len(new) == 2
        assert all(l.id != "L002" for l in new)

    def test_max_edits_enforced(self, monkeypatch):
        monkeypatch.setattr(config, "PLAYBOOK_MAX_EDITS_DAY", 2)
        lessons = _seed_lessons()
        edits = [
            {"op": "remove", "lesson_id": "L001", "text": "", "evidence": []},
            {"op": "remove", "lesson_id": "L002", "text": "", "evidence": []},
            {"op": "remove", "lesson_id": "L003", "text": "", "evidence": []},
        ]
        new, applied, rejected = apply_edits(lessons, edits)
        assert len(applied) == 2
        assert len(rejected) == 1

    def test_apply_edits_returns_rejected(self):
        lessons = _seed_lessons()
        edits = [
            {"op": "add", "lesson_id": "L004", "text": "Good", "evidence": ["D1", "D2"]},
            {"op": "add", "lesson_id": "L005", "text": "Good", "evidence": ["D1", "D2", "D3"]},
        ]
        new, applied, rejected = apply_edits(lessons, edits)
        assert len(applied) == 1
        assert len(rejected) == 1


# ── Journal queries ───────────────────────────────────────────────────────────

class TestJournalReflection:
    def test_get_yesterday_closed_trades(self):
        _insert_trade_with_outcome(reasoning="yesterday trade", pnl=-0.5, days_ago=1)
        trades = journal.get_yesterday_closed_trades()
        assert len(trades) == 1
        assert trades[0]["reasoning"] == "yesterday trade"

    def test_get_rolling_stats_empty(self):
        stats = journal.get_rolling_stats(days=7)
        assert stats["total_trades"] == 0

    def test_get_rolling_stats_with_data(self):
        for pnl in [1.0, -0.5, 2.0]:
            _insert_trade_with_outcome(pnl=pnl)
        stats = journal.get_rolling_stats(days=7)
        assert stats["total_trades"] == 3
        assert stats["wins"] == 2
        assert stats["win_rate"] == 66.7

    def test_get_lesson_hit_counts(self):
        _insert_trade_with_outcome(lessons_applied=["L001", "L002"])
        _insert_trade_with_outcome(lessons_applied=["L001"])
        counts = journal.get_lesson_hit_counts(since_days=30)
        assert counts["L001"] == 2
        assert counts["L002"] == 1

    def test_record_reflection(self):
        rid = journal.record_reflection(
            edits=[{"op": "add", "text": "test"}],
            trades_reviewed=5, edits_applied=1, edits_rejected=0,
            model="test", prompt_tokens=100, completion_tokens=50,
        )
        assert rid > 0
        recent = journal.get_recent_reflections(limit=1)
        assert len(recent) == 1
        assert recent[0]["trades_reviewed"] == 5

    def test_lessons_applied_storage(self):
        did = journal.record_decision(
            briefing={}, action="hold", confidence=0, size_multiplier=0,
            reasoning="test", invalidation_price=None, model="test",
            lessons_applied=["L001", "L003"],
        )
        conn = sqlite3.connect(str(journal.DB_PATH))
        row = conn.execute(
            "SELECT lessons_applied_json FROM decisions WHERE id = ?", (did,)
        ).fetchone()
        conn.close()
        assert json.loads(row[0]) == ["L001", "L003"]


# ── Lesson decay ──────────────────────────────────────────────────────────────

class TestLessonDecay:
    def test_decay_removes_unused(self):
        lessons = _seed_lessons()
        hit_counts = {"L001": 0, "L002": 0, "L003": 5}
        surviving, removed = apply_decay(lessons, hit_counts)
        # L002 is not a seed lesson and has 0 hits → removed
        assert "L002" in removed
        # L003 has hits → kept
        assert any(l.id == "L003" for l in surviving)

    def test_decay_keeps_seed_lessons(self):
        lessons = _seed_lessons()
        hit_counts = {"L001": 0, "L002": 0, "L003": 0}
        surviving, removed = apply_decay(lessons, hit_counts)
        # L001 is a seed lesson (evidence=['none']) → kept
        assert any(l.id == "L001" for l in surviving)
        assert "L001" not in removed

    def test_decay_keeps_used(self):
        lessons = _seed_lessons()
        hit_counts = {"L001": 0, "L002": 3, "L003": 1}
        surviving, removed = apply_decay(lessons, hit_counts)
        assert len(removed) == 0
        assert len(surviving) == 3


# ── Reflection response parsing ───────────────────────────────────────────────

class TestReflectionParsing:
    def test_parse_tool_call_response(self):
        response = {
            "tool_calls": [{
                "id": "call_1",
                "name": "submit_reflection",
                "arguments": json.dumps({
                    "trade_reviews": [],
                    "repeated_mistakes": [],
                    "edits": [{"op": "add", "lesson_id": "L010", "text": "Rule", "evidence": ["D1", "D2", "D3"]}],
                }),
            }],
            "content": "",
        }
        result = parse_reflection_response(response)
        assert len(result["edits"]) == 1

    def test_parse_json_fallback(self):
        response = {
            "tool_calls": [],
            "content": json.dumps({
                "trade_reviews": [],
                "repeated_mistakes": [],
                "edits": [],
            }),
        }
        result = parse_reflection_response(response)
        assert result["edits"] == []

    def test_parse_empty_raises(self):
        with pytest.raises(ValueError):
            parse_reflection_response({"tool_calls": [], "content": ""})


# ── Reflection flow (mocked LLM) ─────────────────────────────────────────────

class TestReflectionFlow:
    def test_no_trades_early_return(self, tmp_playbook):
        """No yesterday trades → LLM not called, returns early."""
        save_lessons(_seed_lessons(), tmp_playbook)
        with patch("reflect.llm_client.complete") as mock_api:
            result = run_reflection(dry_run=True, playbook_path=tmp_playbook)
        mock_api.assert_not_called()
        assert result["trades_reviewed"] == 0
        assert result["error"] is None

    def test_reflection_applies_valid_edits(self, tmp_playbook):
        """Mock LLM returns valid edits → they get applied."""
        _insert_trade_with_outcome(reasoning="test trade", pnl=-1.0, days_ago=1)
        save_lessons(_seed_lessons(), tmp_playbook)

        mock_response = {
            "tool_calls": [{
                "id": "call_1",
                "name": "submit_reflection",
                "arguments": json.dumps({
                    "trade_reviews": [{"decision_id": 1, "verdict": "flawed", "explanation": "bad"}],
                    "repeated_mistakes": [],
                    "edits": [{
                        "op": "add",
                        "lesson_id": "L004",
                        "text": "Avoid entering during high volatility.",
                        "evidence": ["1", "1", "1"],
                    }],
                }),
            }],
            "content": "",
            "model": "deepseek-v4-pro",
            "prompt_tokens": 5000,
            "completion_tokens": 500,
        }

        with patch("reflect.llm_client.complete", return_value=mock_response):
            result = run_reflection(dry_run=False, playbook_path=tmp_playbook)

        assert result["edits_applied"] == 1
        assert result["error"] is None

        # Verify saved
        loaded = load_lessons(tmp_playbook)
        ids = [l.id for l in loaded]
        assert "L004" in ids

    def test_reflection_rejects_overfit_edits(self, tmp_playbook):
        """Mock LLM returns edits with < 3 evidence → rejected."""
        _insert_trade_with_outcome(reasoning="test", pnl=-1.0, days_ago=1)
        save_lessons(_seed_lessons(), tmp_playbook)

        mock_response = {
            "tool_calls": [{
                "id": "call_1",
                "name": "submit_reflection",
                "arguments": json.dumps({
                    "trade_reviews": [],
                    "repeated_mistakes": [],
                    "edits": [{
                        "op": "add",
                        "lesson_id": "L004",
                        "text": "Bad lesson",
                        "evidence": ["1"],  # < 3
                    }],
                }),
            }],
            "content": "",
            "model": "test",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        with patch("reflect.llm_client.complete", return_value=mock_response):
            result = run_reflection(dry_run=False, playbook_path=tmp_playbook)

        assert result["edits_applied"] == 0
        assert result["edits_rejected"] == 1

    def test_reflection_dry_run_no_write(self, tmp_playbook):
        """Dry run should not modify the playbook."""
        _insert_trade_with_outcome(reasoning="test", pnl=-1.0, days_ago=1)
        original = _seed_lessons()
        save_lessons(original, tmp_playbook)

        mock_response = {
            "tool_calls": [{
                "id": "call_1",
                "name": "submit_reflection",
                "arguments": json.dumps({
                    "trade_reviews": [],
                    "repeated_mistakes": [],
                    "edits": [{
                        "op": "add",
                        "lesson_id": "L004",
                        "text": "Should not be saved",
                        "evidence": ["1", "2", "3"],
                    }],
                }),
            }],
            "content": "",
            "model": "test",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        with patch("reflect.llm_client.complete", return_value=mock_response):
            result = run_reflection(dry_run=True, playbook_path=tmp_playbook)

        assert result["edits_applied"] == 1
        # Playbook unchanged
        loaded = load_lessons(tmp_playbook)
        assert len(loaded) == 3  # still just seed lessons

    def test_reflection_api_error(self, tmp_playbook):
        """API error → returns error, no crash."""
        _insert_trade_with_outcome(reasoning="test", pnl=-1.0, days_ago=1)
        save_lessons(_seed_lessons(), tmp_playbook)

        with patch("reflect.llm_client.complete", side_effect=TimeoutError("timeout")):
            result = run_reflection(dry_run=False, playbook_path=tmp_playbook)

        assert result["error"] is not None
        assert "timeout" in result["error"]


# ── Build reflection prompt ───────────────────────────────────────────────────

class TestBuildPrompt:
    def test_prompt_contains_required_fields(self):
        trades = [{"decision_id": 1, "action": "open_long", "reasoning": "test",
                   "confidence": 0.8, "lessons_applied_json": "[]",
                   "entry_price": 100, "exit_price": 101, "pnl_usd": 1.0,
                   "pnl_pct": 1.0, "exit_reason": "signal"}]
        stats = {"total_trades": 1, "win_rate": 100}
        lessons = _seed_lessons()
        prompt = build_reflection_prompt(trades, stats, lessons, [])
        data = json.loads(prompt)
        assert "yesterday_trades" in data
        assert "rolling_stats" in data
        assert "current_playbook" in data
        assert len(data["yesterday_trades"]) == 1
