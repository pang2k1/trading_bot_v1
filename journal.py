"""
journal.py
──────────
SQLite journal for LLM trading decisions and outcomes.

Tables:
    decisions — one row per LLM decision call
    outcomes  — linked to decisions via FK, filled on position close

The journal.db file is gitignored (contains account/trade history).
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path("journal.db")


def _connect() -> sqlite3.Connection:
    """Open (or create) the journal database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,
            briefing_json   TEXT,
            action          TEXT    NOT NULL,
            confidence      REAL,
            size_multiplier REAL,
            reasoning       TEXT,
            invalidation_price REAL,
            model           TEXT,
            prompt_tokens   INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            executed        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id         INTEGER NOT NULL REFERENCES decisions(id),
            entry_price         REAL,
            exit_price          REAL,
            entry_time          TEXT,
            exit_time           TEXT,
            pnl_usd             REAL,
            pnl_pct             REAL,
            exit_reason         TEXT,
            max_adverse_excursion  REAL DEFAULT 0,
            max_favorable_excursion REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reflections (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT    NOT NULL,
            edits_json          TEXT    NOT NULL,
            trades_reviewed     INTEGER DEFAULT 0,
            edits_applied       INTEGER DEFAULT 0,
            edits_rejected      INTEGER DEFAULT 0,
            model               TEXT,
            prompt_tokens       INTEGER DEFAULT 0,
            completion_tokens   INTEGER DEFAULT 0
        );
    """)
    # Safe migrations — add columns if they don't exist yet
    try:
        conn.execute("ALTER TABLE decisions ADD COLUMN lessons_applied_json TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def record_decision(
    briefing: dict,
    action: str,
    confidence: float,
    size_multiplier: float,
    reasoning: str,
    invalidation_price: float | None,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    executed: bool = False,
    lessons_applied: list[str] | None = None,
) -> int:
    """Record an LLM decision. Returns the decision ID."""
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO decisions
               (timestamp, briefing_json, action, confidence, size_multiplier,
                reasoning, invalidation_price, model, prompt_tokens,
                completion_tokens, executed, lessons_applied_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(briefing, ensure_ascii=False),
                action,
                confidence,
                size_multiplier,
                reasoning,
                invalidation_price,
                model,
                prompt_tokens,
                completion_tokens,
                1 if executed else 0,
                json.dumps(lessons_applied or []),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_outcome(
    decision_id: int,
    entry_price: float,
    exit_price: float,
    entry_time: str,
    exit_time: str,
    pnl_usd: float,
    exit_reason: str,
    max_adverse: float = 0.0,
    max_favorable: float = 0.0,
) -> int:
    """Record the outcome of a decision after a position closes."""
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO outcomes
               (decision_id, entry_price, exit_price, entry_time, exit_time,
                pnl_usd, pnl_pct, exit_reason, max_adverse_excursion,
                max_favorable_excursion)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id,
                entry_price,
                exit_price,
                entry_time,
                exit_time,
                round(pnl_usd, 4),
                round(pnl_pct, 4),
                exit_reason,
                round(max_adverse, 4),
                round(max_favorable, 4),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_recent_decisions(limit: int = 50) -> list[dict]:
    """Fetch recent decisions with their outcomes, if any."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT d.*, o.entry_price, o.exit_price, o.entry_time,
                      o.exit_time, o.pnl_usd, o.pnl_pct, o.exit_reason
               FROM decisions d
               LEFT JOIN outcomes o ON o.decision_id = d.id
               ORDER BY d.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_decisions_without_outcomes(limit: int = 100) -> list[dict]:
    """Find decisions that opened positions but have no recorded outcome yet."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT d.* FROM decisions d
               LEFT JOIN outcomes o ON o.decision_id = d.id
               WHERE d.action IN ('open_long', 'open_short')
                 AND o.id IS NULL
               ORDER BY d.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_performance_stats(days: int = 30) -> dict:
    """Aggregate performance statistics for recent decisions."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN o.pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN o.pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                 COALESCE(SUM(o.pnl_usd), 0) as total_pnl,
                 COALESCE(SUM(CASE WHEN o.pnl_usd > 0 THEN o.pnl_usd ELSE 0 END), 0) as gross_profit,
                 COALESCE(SUM(CASE WHEN o.pnl_usd <= 0 THEN o.pnl_usd ELSE 0 END), 0) as gross_loss
               FROM decisions d
               JOIN outcomes o ON o.decision_id = d.id
               WHERE d.timestamp >= datetime('now', ?)""",
            (f"-{days} days",),
        ).fetchone()

        if not row or row["total_trades"] == 0:
            return {"total_trades": 0, "win_rate": 0, "profit_factor": 0, "total_pnl": 0}

        total = row["total_trades"]
        wins = row["wins"] or 0
        gp = abs(row["gross_profit"]) if row["gross_profit"] else 0
        gl = abs(row["gross_loss"]) if row["gross_loss"] else 0

        return {
            "total_trades": total,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "profit_factor": round(gp / gl, 2) if gl > 0 else float("inf"),
            "total_pnl": round(row["total_pnl"], 4),
            "gross_profit": round(gp, 4),
            "gross_loss": round(gl, 4),
        }
    finally:
        conn.close()


def get_monthly_token_spend() -> dict:
    """Total tokens used this calendar month."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                 COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                 COUNT(*) as total_calls
               FROM decisions
               WHERE timestamp >= datetime('now', 'start of month')"""
        ).fetchone()
        return dict(row) if row else {"prompt_tokens": 0, "completion_tokens": 0, "total_calls": 0}
    finally:
        conn.close()


def get_yesterday_closed_trades() -> list[dict]:
    """Fetch yesterday's closed trades with original reasoning + outcomes."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT d.id as decision_id, d.timestamp, d.action, d.confidence,
                      d.size_multiplier, d.reasoning, d.invalidation_price,
                      d.lessons_applied_json,
                      o.entry_price, o.exit_price, o.entry_time, o.exit_time,
                      o.pnl_usd, o.pnl_pct, o.exit_reason,
                      o.max_adverse_excursion, o.max_favorable_excursion
               FROM decisions d
               JOIN outcomes o ON o.decision_id = d.id
               WHERE d.action IN ('open_long', 'open_short')
                 AND d.timestamp >= datetime('now', '-1 day', 'start of day')
                 AND d.timestamp < datetime('now', 'start of day')
               ORDER BY d.timestamp ASC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_rolling_stats(days: int = 7) -> dict:
    """Rolling performance stats for reflection context."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN o.pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                 COALESCE(SUM(o.pnl_usd), 0) as total_pnl,
                 COALESCE(AVG(o.pnl_pct), 0) as avg_pnl_pct,
                 COALESCE(AVG(CASE WHEN o.pnl_usd > 0 THEN o.pnl_pct END), 0) as avg_win_pct,
                 COALESCE(AVG(CASE WHEN o.pnl_usd <= 0 THEN o.pnl_pct END), 0) as avg_loss_pct,
                 COALESCE(MIN(o.pnl_usd), 0) as worst_trade,
                 COALESCE(MAX(o.pnl_usd), 0) as best_trade
               FROM decisions d
               JOIN outcomes o ON o.decision_id = d.id
               WHERE d.action IN ('open_long', 'open_short')
                 AND d.timestamp >= datetime('now', ?)""",
            (f"-{days} days",),
        ).fetchone()

        if not row or row["total_trades"] == 0:
            return {"total_trades": 0}

        total = row["total_trades"]
        wins = row["wins"] or 0
        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total * 100, 1),
            "total_pnl": round(row["total_pnl"], 4),
            "avg_pnl_pct": round(row["avg_pnl_pct"], 2),
            "avg_win_pct": round(row["avg_win_pct"], 2),
            "avg_loss_pct": round(row["avg_loss_pct"], 2),
            "worst_trade": round(row["worst_trade"], 4),
            "best_trade": round(row["best_trade"], 4),
        }
    finally:
        conn.close()


def get_lesson_hit_counts(since_days: int = 30) -> dict[str, int]:
    """Count how many times each lesson was applied in decisions."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT lessons_applied_json
               FROM decisions
               WHERE timestamp >= datetime('now', ?)
                 AND lessons_applied_json IS NOT NULL""",
            (f"-{since_days} days",),
        ).fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            try:
                ids = json.loads(row["lessons_applied_json"])
                for lid in ids:
                    counts[lid] = counts.get(lid, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        return counts
    finally:
        conn.close()


def record_reflection(
    edits: list[dict],
    trades_reviewed: int = 0,
    edits_applied: int = 0,
    edits_rejected: int = 0,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> int:
    """Record a reflection run for auditing."""
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO reflections
               (timestamp, edits_json, trades_reviewed, edits_applied,
                edits_rejected, model, prompt_tokens, completion_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(edits, ensure_ascii=False),
                trades_reviewed,
                edits_applied,
                edits_rejected,
                model,
                prompt_tokens,
                completion_tokens,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_recent_reflections(limit: int = 30) -> list[dict]:
    """Fetch recent reflection runs for context."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM reflections ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
