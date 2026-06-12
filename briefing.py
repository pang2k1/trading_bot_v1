"""
briefing.py
───────────
Assembles one JSON "market briefing" per cycle for the LLM decision engine.

Collects: technical indicators, rule-engine signals, news sentiment,
account state, and memory (recent trades + playbook).
Keeps the briefing under ~3,000 tokens — derived features only, no raw candles.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
import indicators
import strategy
import news_analyzer

log = logging.getLogger(__name__)

PLAYBOOK_FILE = Path("playbook.md")
PLAYBOOK_EXAMPLE = Path("playbook.example.md")
TRADES_LOG = Path("trades_log.csv")
MAX_RECENT_TRADES = 10


def build_technical(df: pd.DataFrame) -> dict:
    """Extract derived technical features from the last row of an indicator DataFrame."""
    if df.empty:
        return {}
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    close = float(last["close"])
    bb_upper = float(last["bb_upper"])
    bb_mid = float(last["bb_mid"])
    bb_lower = float(last["bb_lower"])
    rsi = float(last["rsi"])

    # BB position: 0 = at lower band, 1 = at upper band
    bb_range = bb_upper - bb_lower
    bb_position = (close - bb_lower) / bb_range if bb_range > 0 else 0.5

    # Distance to bands as percentage of close
    dist_to_upper = (bb_upper - close) / close * 100 if close > 0 else 0
    dist_to_lower = (close - bb_lower) / close * 100 if close > 0 else 0

    # Recent volatility: ATR-like (using high-low range of last 5 bars)
    recent = df.tail(5)
    avg_range = (recent["high"] - recent["low"]).mean()
    volatility_pct = avg_range / close * 100 if close > 0 else 0

    # Trend biases
    trend1_bias = int(last.get("trend1_bias", 0))
    trend2_bias = int(last.get("trend2_bias", 0))
    prev_trend1 = int(prev.get("trend1_bias", 0))
    prev_trend2 = int(prev.get("trend2_bias", 0))

    return {
        "price": round(close, 2),
        "bb_position": round(bb_position, 4),
        "bb_upper": round(bb_upper, 2),
        "bb_mid": round(bb_mid, 2),
        "bb_lower": round(bb_lower, 2),
        "dist_to_upper_pct": round(dist_to_upper, 3),
        "dist_to_lower_pct": round(dist_to_lower, 3),
        "rsi": round(rsi, 1),
        "prev_rsi": round(float(prev["rsi"]), 1),
        "trend_15m": "bullish" if trend1_bias == 1 else "bearish",
        "trend_1h": "bullish" if trend2_bias == 1 else "bearish",
        "prev_trend_15m": "bullish" if prev_trend1 == 1 else "bearish",
        "prev_trend_1h": "bullish" if prev_trend2 == 1 else "bearish",
        "volatility_pct": round(volatility_pct, 3),
    }


def build_rule_signals(df: pd.DataFrame) -> dict:
    """Which rule-engine signals fired (becomes an input feature, not the decider)."""
    df = strategy.generate_signals(df)
    last = df.iloc[-1]
    return {
        "long_entry": bool(last.get("long_entry", False)),
        "long_exit": bool(last.get("long_exit", False)),
        "short_entry": bool(last.get("short_entry", False)),
        "short_exit": bool(last.get("short_exit", False)),
    }


def build_news(symbol: str, news_cache: dict[str, float] | None = None) -> dict:
    """News sentiment summary for the briefing."""
    if news_cache and symbol in news_cache:
        score = news_cache[symbol]
    else:
        try:
            scores = news_analyzer.get_market_sentiment([symbol])
            score = scores.get(symbol, 0.0)
        except Exception:
            score = 0.0

    # Try to get top headlines
    headlines = []
    try:
        articles = news_analyzer.fetch_all_news(max_age_hours=24)
        analysis = news_analyzer.analyze_symbol(articles, symbol)
        headlines = [
            {
                "title": h["title"][:100],
                "sentiment": h["sentiment"],
            }
            for h in analysis.get("top_headlines", [])[:5]
        ]
    except Exception:
        pass

    return {
        "sentiment_score": round(score, 4),
        "top_headlines": headlines,
    }


def build_account(
    balance: float,
    state: dict | None = None,
    circuit_daily_pnl: float = 0.0,
    circuit_start_balance: float = 0.0,
) -> dict:
    """Account and position state."""
    result = {
        "balance_usdt": round(balance, 4),
        "daily_pnl_usdt": round(circuit_daily_pnl, 4),
        "daily_loss_limit_pct": config.MAX_DAILY_LOSS_PCT * 100,
    }

    if circuit_start_balance > 0:
        loss_pct = -circuit_daily_pnl / circuit_start_balance * 100
        result["daily_loss_used_pct"] = round(loss_pct, 2)
    else:
        result["daily_loss_used_pct"] = 0.0

    # Open position info (first symbol only — bot trades one symbol)
    if state:
        for symbol, pos in state.items():
            entry = float(pos["entry_price"])
            qty = float(pos["qty"])
            notional = float(pos.get("notional", entry * qty))
            unrealized = 0.0
            # We don't have current price here; caller should patch it
            result["open_position"] = {
                "side": pos["side"],
                "entry_price": round(entry, 2),
                "qty": qty,
                "notional_usdt": round(notional, 2),
                "stop_loss": round(float(pos["stop_loss"]), 2),
                "entry_time": pos["entry_time"],
                "unrealized_pnl_usdt": round(unrealized, 4),
            }
            break
    else:
        result["open_position"] = None

    return result


def build_memory() -> dict:
    """Last N closed trades and current playbook content."""
    recent_trades = []
    if TRADES_LOG.exists():
        try:
            df = pd.read_csv(TRADES_LOG)
            df = df.tail(MAX_RECENT_TRADES)
            for _, row in df.iterrows():
                recent_trades.append({
                    "side": str(row.get("side", "")),
                    "entry_price": round(float(row.get("entry_price", 0)), 2),
                    "exit_price": round(float(row.get("exit_price", 0)), 2),
                    "pnl_usd": round(float(row.get("pnl_usd", 0)), 4),
                    "reason": str(row.get("reason", "")),
                })
        except Exception:
            pass

    playbook_text = ""
    playbook_path = PLAYBOOK_FILE if PLAYBOOK_FILE.exists() else PLAYBOOK_EXAMPLE
    if playbook_path.exists():
        try:
            playbook_text = playbook_path.read_text(encoding="utf-8")[:2000]
        except Exception:
            pass

    return {
        "recent_trades": recent_trades,
        "playbook": playbook_text if playbook_text else "No playbook yet.",
    }


def assemble_briefing(
    df: pd.DataFrame,
    symbol: str,
    balance: float,
    state: dict | None = None,
    news_cache: dict[str, float] | None = None,
    circuit_daily_pnl: float = 0.0,
    circuit_start_balance: float = 0.0,
) -> dict:
    """Assemble the full market briefing JSON."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "timeframe": config.BASE_TF,
        "technical": build_technical(df),
        "rule_engine_signals": build_rule_signals(df),
        "news": build_news(symbol, news_cache),
        "account": build_account(balance, state, circuit_daily_pnl, circuit_start_balance),
        "memory": build_memory(),
    }


def briefing_to_text(briefing: dict) -> str:
    """Serialize briefing to a concise text for the LLM prompt."""
    return json.dumps(briefing, indent=2, ensure_ascii=False)
