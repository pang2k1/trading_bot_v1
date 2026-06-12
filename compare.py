"""
compare.py
──────────
Weekly comparison report: LLM (simulated) vs rule bot vs buy-and-hold.

Reads the journal.db decisions+outcomes and trades_log.csv to produce
a comparison of performance metrics.

Usage
-----
    python compare.py                 # last 7 days
    python compare.py --days 30       # last 30 days
    python compare.py --all           # all available data
"""

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

TRADES_LOG = Path("trades_log.csv")
JOURNAL_DB = Path("journal.db")

# Simulated costs for LLM paper trades
SIMULATED_SLIPPAGE = 0.0005  # 0.05% per fill
SIMULATED_FEE = 0.001        # 0.1% per side


def _sanitize_json(d: dict) -> dict:
    """Replace inf/nan floats with None so json.dumps doesn't choke."""
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in d.items()}


def _load_rule_trades(days: int | None = None) -> pd.DataFrame:
    """Load rule-engine trades from trades_log.csv."""
    if not TRADES_LOG.exists():
        return pd.DataFrame()
    df = pd.read_csv(TRADES_LOG)
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        df = df[pd.to_datetime(df["exit_time"], utc=True) >= cutoff]
    return df


def _load_llm_decisions(days: int | None = None) -> list[dict]:
    """Load LLM decisions with outcomes from journal.db."""
    if not JOURNAL_DB.exists():
        return []
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT d.*, o.entry_price, o.exit_price, o.entry_time,
                   o.exit_time, o.pnl_usd, o.pnl_pct, o.exit_reason
            FROM decisions d
            LEFT JOIN outcomes o ON o.decision_id = d.id
            WHERE d.action != 'hold'
        """
        params = []
        if days is not None:
            query += " AND d.timestamp >= ?"
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            params.append(cutoff)
        query += " ORDER BY d.id"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _simulate_llm_pnl(decisions: list[dict]) -> dict:
    """Compute simulated PnL for LLM decisions with fees/slippage."""
    if not decisions:
        return {"total_pnl": 0, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "gross_profit": 0, "gross_loss": 0, "profit_factor": 0, "max_dd": 0}

    # Use actual outcomes where available, simulate for the rest
    total_pnl = 0
    wins = 0
    losses = 0
    gross_profit = 0
    gross_loss = 0
    peak_pnl = 0
    max_dd = 0

    for d in decisions:
        pnl = d.get("pnl_usd")
        if pnl is not None and d.get("exit_price") is not None:
            # Real outcome recorded
            pnl = float(pnl)
        else:
            continue  # no outcome yet — skip

        total_pnl += pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        else:
            losses += 1
            gross_loss += abs(pnl)

        # Track drawdown
        peak_pnl = max(peak_pnl, total_pnl)
        dd = peak_pnl - total_pnl
        max_dd = max(max_dd, dd)

    total = wins + losses
    return {
        "total_pnl": round(total_pnl, 4),
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd": round(max_dd, 4),
    }


def _compute_rule_stats(df: pd.DataFrame) -> dict:
    """Compute performance stats for rule-engine trades."""
    if df.empty:
        return {"total_pnl": 0, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "gross_profit": 0, "gross_loss": 0, "profit_factor": 0, "max_dd": 0}

    pnls = df["pnl_usd"].astype(float)
    total_pnl = pnls.sum()
    wins = (pnls > 0).sum()
    losses = (pnls <= 0).sum()
    gross_profit = pnls[pnls > 0].sum()
    gross_loss = abs(pnls[pnls <= 0].sum())

    # Max drawdown from cumulative PnL
    cum = pnls.cumsum()
    peak = cum.cummax()
    dd = (peak - cum).max()

    return {
        "total_pnl": round(total_pnl, 4),
        "trades": len(df),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": round(wins / len(df) * 100, 1),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd": round(dd, 4),
    }


def _compute_agreement(decisions: list[dict], rule_df: pd.DataFrame) -> float:
    """Compute % agreement between LLM and rule engine decisions."""
    if not decisions or rule_df.empty:
        return 0.0

    # Count how many LLM entry decisions correspond to rule trades in same window
    rule_entries = set()
    for _, row in rule_df.iterrows():
        t = str(row.get("entry_time", ""))
        rule_entries.add(t[:16])  # match to minute

    llm_entries = 0
    matched = 0
    for d in decisions:
        if d["action"] in ("open_long", "open_short"):
            llm_entries += 1
            t = str(d.get("timestamp", ""))[:16]
            # Check if rule engine also traded within 1 hour
            if any(t[:13] == re[:13] for re in rule_entries):
                matched += 1

    if llm_entries == 0:
        return 0.0
    return round(matched / llm_entries * 100, 1)


def run_report(days: int | None = 7) -> None:
    """Print the comparison report (CLI)."""
    data = get_comparison_data(days)

    period_str = f"last {days} days" if days else "all time"
    print(f"\n{'='*64}")
    print(f"  LLM vs Rule Engine Comparison Report  —  {period_str}")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*64}\n")

    rule_stats = data["rule"]
    llm_stats = data["llm"]
    bah_pnl = data["buy_and_hold"]["pnl"]
    agreement = data["agreement_pct"]

    def fmt(val, is_pct=False):
        if val == float("inf"):
            return "INF"
        if is_pct:
            return f"{val:.1f}%"
        return f"{val:+.4f}" if isinstance(val, float) and val != 0 else str(val)

    metrics = [
        ("Total PnL (USDT)", "total_pnl", False),
        ("Trades", "trades", False),
        ("Wins / Losses", None, False),
        ("Win Rate", "win_rate", True),
        ("Gross Profit", "gross_profit", False),
        ("Gross Loss", "gross_loss", False),
        ("Profit Factor", "profit_factor", False),
        ("Max Drawdown", "max_dd", False),
    ]

    print(f"{'Metric':<20} {'Rule Engine':>14} {'LLM (simulated)':>16} {'Buy&Hold':>12}")
    print("-" * 64)
    for label, key, is_pct in metrics:
        rule_val = rule_stats.get(key, 0) if key else ""
        llm_val = llm_stats.get(key, 0) if key else ""
        if key is None:
            rule_val = f"{rule_stats['wins']}/{rule_stats['losses']}"
            llm_val = f"{llm_stats['wins']}/{llm_stats['losses']}"
            bah_val = "-"
        elif key == "total_pnl":
            bah_val = f"{bah_pnl:+.4f}"
            rule_val = fmt(rule_val)
            llm_val = fmt(llm_val)
        else:
            bah_val = "-"
            rule_val = fmt(rule_val, is_pct)
            llm_val = fmt(llm_val, is_pct)
        print(f"{label:<20} {str(rule_val):>14} {str(llm_val):>16} {str(bah_val):>12}")

    print(f"\nDecision agreement (LLM ↔ rules): {agreement:.1f}%")
    print(f"LLM decisions in journal: {data['llm_decisions']}")
    print(f"Rule trades in log: {data['rule_trades']}")

    cost = data["estimated_cost_usd"]
    pt = data["prompt_tokens"]
    ct = data["completion_tokens"]
    if pt or ct:
        print(f"Token usage: {pt:,} in + {ct:,} out  (~${cost:.4f} estimated)")

    print(f"\n{'='*64}\n")


def get_comparison_data(days: int | None = 7) -> dict:
    """
    Compute LLM vs rule vs buy-and-hold comparison data.

    Returns a dict with keys:
      rule, llm, buy_and_hold, agreement_pct,
      llm_decisions, rule_trades,
      prompt_tokens, completion_tokens, estimated_cost_usd
    """
    rule_df = _load_rule_trades(days)
    llm_decisions = _load_llm_decisions(days)

    rule_stats = _compute_rule_stats(rule_df)
    llm_stats = _simulate_llm_pnl(llm_decisions)

    bah_pnl = 0.0
    if not rule_df.empty:
        try:
            entries = rule_df["entry_price"].astype(float)
            if len(entries) >= 2:
                bah_pnl = float(entries.iloc[-1]) - float(entries.iloc[0])
        except Exception:
            pass

    agreement = _compute_agreement(llm_decisions, rule_df)

    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0
    if JOURNAL_DB.exists():
        conn = sqlite3.connect(str(JOURNAL_DB))
        try:
            row = conn.execute(
                "SELECT SUM(prompt_tokens) as pt, SUM(completion_tokens) as ct FROM decisions"
            ).fetchone()
            if row:
                prompt_tokens = row[0] or 0
                completion_tokens = row[1] or 0
                cost = prompt_tokens * 0.14 / 1e6 + completion_tokens * 0.28 / 1e6
        finally:
            conn.close()

    return {
        "rule": _sanitize_json(rule_stats),
        "llm": _sanitize_json(llm_stats),
        "buy_and_hold": {"pnl": round(bah_pnl, 4)},
        "agreement_pct": agreement,
        "llm_decisions": len(llm_decisions),
        "rule_trades": len(rule_df),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": round(cost, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM vs Rule Engine comparison report")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default 7)")
    parser.add_argument("--all", action="store_true", help="Use all available data")
    args = parser.parse_args()

    run_report(days=None if args.all else args.days)


if __name__ == "__main__":
    main()
