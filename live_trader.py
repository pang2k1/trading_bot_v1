"""
live_trader.py
──────────────
Binance Testnet / Live Margin trading bot.
Supports both LONG and SHORT positions via cross-margin borrowing.

Setup
-----
1. Testnet keys: https://testnet.binance.vision/
   Note: Binance spot testnet has limited margin support.
   Full margin features (shorts, borrowing) require live keys.
2. Live keys:    https://www.binance.com/en/my/settings/api-management
   - Enable "Spot & Margin Trading" permission
   - Enable Margin trading on your account (Binance → Wallet → Margin)
   - Whitelist your server's IP address
3. Create .env in this directory:

       TESTNET_API_KEY=your_testnet_key
       TESTNET_SECRET=your_testnet_secret

       # For real money (keep commented until ready):
       # LIVE_API_KEY=your_live_key
       # LIVE_SECRET=your_live_secret

Usage
-----
    python live_trader.py               # run continuously on testnet
    python live_trader.py --once        # single cycle (good for testing)
    python live_trader.py --live        # REAL MONEY — requires LIVE_API_KEY + LIVE_SECRET
    python live_trader.py --brain shadow  # shadow mode: LLM journals but doesn't trade (default)
    python live_trader.py --brain rules   # disable LLM, rule engine only
    python live_trader.py --brain llm     # LLM drives trades (Phase 3 — not yet safe)

Notes
-----
- Uses Binance Cross Margin (defaultType: "margin") — supports longs and shorts.
- Shorts auto-borrow the base asset (sideEffectType: MARGIN_BUY) and auto-repay on close.
- Leverage is implicit: determined by borrowed amount relative to your own balance.
  Binance cross-margin allows up to 3x for most assets.
- Loads optimized parameters from best_params.json if it exists.
  Run optimizer.py first to generate it.
- Open positions survive restarts via trader_state.json.
  On startup, positions are validated against live margin balances/debt.
- Daily loss circuit breaker: halts trading if MAX_DAILY_LOSS_PCT is exceeded.
  Resets at UTC midnight.
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv

import config
import indicators
import strategy
import news_analyzer
import llm_trader
import journal as llm_journal

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "live_trader.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────
STATE_FILE  = Path("trader_state.json")
PARAMS_FILE = Path("best_params.json")
TRADES_LOG  = Path("trades_log.csv")

# Seconds per timeframe — used for scheduling
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


# ── Parameter loading ─────────────────────────────────────────────────────────

# Only these indicator/risk params may be overridden by best_params.json.
# API keys, symbols, timeframes, and other settings are never touched.
_TUNABLE_PARAMS = frozenset({
    "BB_PERIOD", "BB_STD",
    "RSI_PERIOD", "RSI_LONG_ENTRY", "RSI_SHORT_ENTRY", "RSI_LONG_EXIT", "RSI_SHORT_EXIT",
    "EMA_TREND1", "EMA_TREND2",
    "STOP_LOSS_PCT", "RISK_PER_TRADE", "MAX_DAILY_LOSS_PCT",
})


def _load_best_params() -> None:
    """Apply optimized params from best_params.json to config (if file exists)."""
    if not PARAMS_FILE.exists():
        log.info("No best_params.json found — using config.py defaults.")
        return
    try:
        with open(PARAMS_FILE) as f:
            params = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read best_params.json ({exc}) — keeping current params.")
        return
    applied = []
    for k, v in params.items():
        if k not in _TUNABLE_PARAMS:
            log.warning(f"Ignoring non-tunable param in best_params.json: {k}")
            continue
        if not isinstance(v, (int, float)):
            log.warning(f"Ignoring param with non-numeric value: {k}={v!r}")
            continue
        setattr(config, k, v)
        applied.append(f"{k}={v}")
    if applied:
        log.info(f"Loaded optimized params: {', '.join(applied)}")


# ── Exchange connection ────────────────────────────────────────────────────────

def _connect(testnet: bool = True) -> ccxt.binance:
    load_dotenv()
    if testnet:
        api_key = os.getenv("TESTNET_API_KEY", "")
        secret  = os.getenv("TESTNET_SECRET", "")
        if not api_key or not secret:
            raise ValueError(
                "TESTNET_API_KEY and TESTNET_SECRET must be set in .env.\n"
                "Get them from https://testnet.binance.vision/\n"
                "Note: testnet has limited margin support — full margin features require live keys."
            )
    else:
        api_key = os.getenv("LIVE_API_KEY", "")
        secret  = os.getenv("LIVE_SECRET", "")
        if not api_key or not secret:
            raise ValueError("LIVE_API_KEY and LIVE_SECRET must be set in .env for live trading.")

    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "margin",
            "defaultMarginMode": "cross",
        },
    })

    if testnet:
        exchange.set_sandbox_mode(True)

    exchange.load_markets()

    mode = "TESTNET" if testnet else "LIVE"
    log.info(f"Connected to Binance Margin {mode}  margin_type={config.MARGIN_TYPE}")
    return exchange


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load and validate persisted position state (survives restarts)."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read trader_state.json ({exc}) — starting with empty state.")
        return {}

    # Validate each position entry — drop corrupt ones rather than trading on bad data
    valid_state = {}
    required_keys = {"side", "entry_price", "qty", "stop_loss", "entry_time"}
    for symbol, pos in state.items():
        if not isinstance(pos, dict):
            log.warning(f"[{symbol}] Dropping invalid state entry (not a dict).")
            continue
        missing = required_keys - pos.keys()
        if missing:
            log.warning(f"[{symbol}] Dropping state entry — missing keys: {missing}")
            continue
        try:
            float(pos["entry_price"])
            float(pos["qty"])
            float(pos["stop_loss"])
        except (TypeError, ValueError):
            log.warning(f"[{symbol}] Dropping state entry — non-numeric price/qty/stop_loss.")
            continue
        if pos["side"] not in ("long", "short"):
            log.warning(f"[{symbol}] Dropping state entry — unknown side: {pos['side']!r}")
            continue
        valid_state[symbol] = pos

    dropped = len(state) - len(valid_state)
    if dropped:
        log.warning(f"Dropped {dropped} corrupt state entries. Saving cleaned state.")
        _save_state(valid_state)

    return valid_state


def _save_state(state: dict) -> None:
    # Write to a temp file then rename for atomicity — avoids partial writes
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


# ── Daily circuit breaker ─────────────────────────────────────────────────────

class DailyCircuitBreaker:
    """
    Tracks realised + unrealised PnL for the current UTC day.
    Halts trading if total losses exceed config.MAX_DAILY_LOSS_PCT of the
    starting balance for that day. Resets automatically at UTC midnight.
    """

    def __init__(self):
        self._date: str = ""
        self._start_balance: float = 0.0
        self._daily_pnl: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._halted: bool = False

    def reset_if_new_day(self, current_balance: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._date:
            self._date          = today
            self._start_balance = current_balance
            self._daily_pnl     = 0.0
            self._unrealized_pnl = 0.0
            self._halted        = False
            log.info(f"[circuit] New day {today} — daily PnL reset. Start balance: {current_balance:.4f} USDT")

    def record_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl
        self._check_halt()

    def set_unrealized(self, unrealized_pnl: float) -> None:
        self._unrealized_pnl = unrealized_pnl
        self._check_halt()

    def _check_halt(self) -> None:
        total_pnl = self._daily_pnl + self._unrealized_pnl
        loss_pct = -total_pnl / self._start_balance if self._start_balance > 0 else 0
        log.info(
            f"[circuit] PnL: realised={self._daily_pnl:+.4f}  "
            f"unrealised={self._unrealized_pnl:+.4f}  "
            f"total={total_pnl:+.4f} USDT  ({-loss_pct*100:.2f}% of day start)"
        )
        if loss_pct >= config.MAX_DAILY_LOSS_PCT and not self._halted:
            self._halted = True
            log.warning(
                f"[circuit] DAILY LOSS LIMIT REACHED "
                f"({loss_pct*100:.1f}% >= {config.MAX_DAILY_LOSS_PCT*100:.1f}%) — "
                f"trading halted until UTC midnight."
            )

    @property
    def halted(self) -> bool:
        return self._halted


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_recent_bars(
    exchange: ccxt.binance, symbol: str, tf: str, limit: int = 300
) -> pd.DataFrame:
    """
    Fetch the last `limit` CLOSED bars.
    We request limit+1 and drop the final (possibly still-open) bar.
    """
    raw = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit + 1)
    raw = raw[:-1]  # drop current open bar
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="first")]
    return df


# ── Signal computation ────────────────────────────────────────────────────────

def _get_signal(exchange: ccxt.binance, symbol: str) -> tuple[dict, float]:
    """
    Fetch latest bars, compute indicators, generate signals.
    Returns (signal_dict, last_close_price) where signal_dict has boolean keys:
        long_entry, long_exit, short_entry, short_exit.
    """
    frames = {
        config.BASE_TF:   _fetch_recent_bars(exchange, symbol, config.BASE_TF),
        config.TREND_TF1: _fetch_recent_bars(exchange, symbol, config.TREND_TF1),
        config.TREND_TF2: _fetch_recent_bars(exchange, symbol, config.TREND_TF2),
    }
    df = indicators.build(frames)
    df = strategy.generate_signals(df)
    last = df.iloc[-1]
    signals = {
        "long_entry":  bool(last.get("long_entry", False)),
        "long_exit":   bool(last.get("long_exit", False)),
        "short_entry": bool(last.get("short_entry", False)),
        "short_exit":  bool(last.get("short_exit", False)),
    }
    return signals, float(last["close"])


# ── Order execution ───────────────────────────────────────────────────────────

def _get_usdt_balance(exchange: ccxt.binance) -> float:
    balance = exchange.fetch_balance()
    # Margin: CCXT returns available (unborrowed + unused) USDT under ["USDT"]["free"].
    # This is the amount we can use as collateral for new positions.
    usdt = balance.get("USDT") or balance.get("usdt") or {}
    return float(usdt.get("free") or 0)


_MIN_NOTIONAL = 5.5  # Binance minimum is 5 USDT — keep a small buffer

def _calc_qty(exchange: ccxt.binance, symbol: str, price: float) -> float:
    """Size position: equity × RISK_PER_TRADE, min 5.5 USDT notional, rounded to exchange precision.
    Returns 0 if free balance is below min notional."""
    free_balance = _get_usdt_balance(exchange)
    if free_balance < _MIN_NOTIONAL:
        log.warning(f"[{symbol}] Free balance ({free_balance:.2f} USDT) below min notional ({_MIN_NOTIONAL}) — skipping.")
        return 0
    notional = max(free_balance * config.RISK_PER_TRADE, _MIN_NOTIONAL)
    qty      = notional / price
    qty      = float(exchange.amount_to_precision(symbol, qty))
    return qty


def _place_exchange_stop(exchange: ccxt.binance, symbol: str, side: str, qty: float, stop_price: float) -> str | None:
    """Place an exchange-side stop-loss-limit order. Returns order ID or None on failure."""
    try:
        if side == "long":
            # Sell to close long: stop triggers a limit sell below stop
            limit_price = round(stop_price * 0.995, 8)  # limit slightly below stop for fill
            stop_order = exchange.create_order(
                symbol, "stop_loss_limit", "sell", qty, limit_price,
                params={"stopPrice": stop_price, "marginMode": config.MARGIN_TYPE},
            )
        else:
            # Buy to close short: stop triggers a limit buy above stop
            limit_price = round(stop_price * 1.005, 8)
            stop_order = exchange.create_order(
                symbol, "stop_loss_limit", "buy", qty, limit_price,
                params={"stopPrice": stop_price, "marginMode": config.MARGIN_TYPE},
            )
        log.info(f"[{symbol}] Exchange stop-order placed  id={stop_order.get('id')}  SL={stop_price:.4f}")
        return stop_order.get("id")
    except Exception as exc:
        log.warning(f"[{symbol}] Failed to place exchange stop-order: {exc} — relying on bot-side SL check")
        return None


def _cancel_exchange_stop(exchange: ccxt.binance, symbol: str, stop_order_id: str | None) -> None:
    """Cancel an existing exchange-side stop order."""
    if not stop_order_id:
        return
    try:
        exchange.cancel_order(stop_order_id, symbol)
        log.info(f"[{symbol}] Cancelled exchange stop-order {stop_order_id}")
    except Exception as exc:
        log.warning(f"[{symbol}] Could not cancel stop-order {stop_order_id}: {exc}")


def _open_long(exchange: ccxt.binance, symbol: str, price: float, state: dict) -> None:
    qty = _calc_qty(exchange, symbol, price)
    if qty <= 0:
        log.warning(f"[{symbol}] Insufficient balance for long entry — skipping.")
        return

    log.info(f"[{symbol}] Opening LONG  qty={qty}  ~price={price:.4f}")
    order = exchange.create_market_buy_order(symbol, qty, params={"marginMode": config.MARGIN_TYPE})

    fill_price = float(order.get("average") or order.get("price") or price)
    stop_price = round(fill_price * (1 - config.STOP_LOSS_PCT), 8)

    # Place exchange-side stop order
    sl_order_id = _place_exchange_stop(exchange, symbol, "long", qty, stop_price)

    state[symbol] = {
        "side":        "long",
        "entry_price": fill_price,
        "qty":         float(qty),
        "stop_loss":   stop_price,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "notional":    float(qty) * fill_price,
        "order_id":    order.get("id"),
        "sl_order_id": sl_order_id,
    }
    _save_state(state)
    log.info(f"[{symbol}] LONG opened  fill={fill_price:.4f}  SL={stop_price:.4f}")


def _close_long(
    exchange: ccxt.binance, symbol: str, state: dict,
    reason: str = "signal", circuit: "DailyCircuitBreaker | None" = None,
) -> None:
    pos = state.get(symbol)
    if not pos or pos["side"] != "long":
        return

    qty = float(pos["qty"])
    log.info(f"[{symbol}] Closing LONG  qty={qty}  reason={reason}")

    # Cancel exchange-side stop order before closing
    _cancel_exchange_stop(exchange, symbol, pos.get("sl_order_id"))

    order      = exchange.create_market_sell_order(symbol, qty, params={"marginMode": config.MARGIN_TYPE})
    exit_price = float(order.get("average") or order.get("price") or pos["entry_price"])
    pnl        = (exit_price - pos["entry_price"]) * qty

    # Deduct fees from order response if available
    fee = _extract_fee(order)
    if fee > 0:
        pnl -= fee

    log.info(f"[{symbol}] LONG closed  exit={exit_price:.4f}  PnL={pnl:+.4f} USDT")
    _log_trade(pos, exit_price, pnl, reason)
    _record_llm_outcome(pos, exit_price, pnl, reason)
    if circuit is not None:
        circuit.record_pnl(pnl)
    del state[symbol]
    _save_state(state)


def _open_short(exchange: ccxt.binance, symbol: str, price: float, state: dict) -> None:
    qty = _calc_qty(exchange, symbol, price)
    if qty <= 0:
        log.warning(f"[{symbol}] Insufficient balance for short entry — skipping.")
        return

    log.info(f"[{symbol}] Opening SHORT  qty={qty}  ~price={price:.4f}")
    # On margin, MARGIN_BUY auto-borrows the base asset then sells it to open the short
    order = exchange.create_market_sell_order(symbol, qty, params={"sideEffectType": "MARGIN_BUY", "marginMode": config.MARGIN_TYPE})

    fill_price = float(order.get("average") or order.get("price") or price)
    stop_price = round(fill_price * (1 + config.STOP_LOSS_PCT), 8)

    # Place exchange-side stop order
    sl_order_id = _place_exchange_stop(exchange, symbol, "short", qty, stop_price)

    state[symbol] = {
        "side":        "short",
        "entry_price": fill_price,
        "qty":         float(qty),
        "stop_loss":   stop_price,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "notional":    float(qty) * fill_price,
        "order_id":    order.get("id"),
        "sl_order_id": sl_order_id,
    }
    _save_state(state)
    log.info(f"[{symbol}] SHORT opened  fill={fill_price:.4f}  SL={stop_price:.4f}")


def _close_short(
    exchange: ccxt.binance, symbol: str, state: dict,
    reason: str = "signal", circuit: "DailyCircuitBreaker | None" = None,
) -> None:
    pos = state.get(symbol)
    if not pos or pos["side"] != "short":
        return

    qty = float(pos["qty"])
    log.info(f"[{symbol}] Closing SHORT  qty={qty}  reason={reason}")

    # Cancel exchange-side stop order before closing
    _cancel_exchange_stop(exchange, symbol, pos.get("sl_order_id"))

    # AUTO_REPAY buys the base asset and automatically repays the margin loan
    order      = exchange.create_market_buy_order(symbol, qty, params={"sideEffectType": "AUTO_REPAY", "marginMode": config.MARGIN_TYPE})
    exit_price = float(order.get("average") or order.get("price") or pos["entry_price"])
    pnl        = (pos["entry_price"] - exit_price) * qty

    # Deduct fees from order response if available
    fee = _extract_fee(order)
    if fee > 0:
        pnl -= fee

    log.info(f"[{symbol}] SHORT closed  exit={exit_price:.4f}  PnL={pnl:+.4f} USDT")
    _log_trade(pos, exit_price, pnl, reason)
    _record_llm_outcome(pos, exit_price, pnl, reason)
    if circuit is not None:
        circuit.record_pnl(pnl)
    del state[symbol]
    _save_state(state)


def _extract_fee(order: dict) -> float:
    """Extract fee cost from an order response. Returns 0 if not found."""
    fees = order.get("fees") or []
    if isinstance(fees, list) and fees:
        return sum(float(f.get("cost", 0)) for f in fees)
    fee = order.get("fee") or {}
    return float(fee.get("cost", 0))


def _log_trade(pos: dict, exit_price: float, pnl: float, reason: str) -> None:
    """Append a completed trade to trades_log.csv."""
    row = {
        "side":        pos["side"],
        "entry_time":  pos["entry_time"],
        "exit_time":   datetime.now(timezone.utc).isoformat(),
        "entry_price": pos["entry_price"],
        "exit_price":  exit_price,
        "qty":         pos["qty"],
        "pnl_usd":     round(pnl, 4),
        "reason":      reason,
    }
    header = not TRADES_LOG.exists()
    pd.DataFrame([row]).to_csv(TRADES_LOG, mode="a", header=header, index=False)


# ── LLM journal outcome hook ──────────────────────────────────────────────────

def _record_llm_outcome(pos: dict, exit_price: float, pnl: float, reason: str) -> None:
    """Record the outcome for any pending LLM decision that opened this position."""
    try:
        pending = llm_journal.get_decisions_without_outcomes(limit=10)
        for d in pending:
            if d.get("action") in ("open_long", "open_short"):
                llm_journal.record_outcome(
                    decision_id=d["id"],
                    entry_price=float(pos["entry_price"]),
                    exit_price=exit_price,
                    entry_time=pos["entry_time"],
                    exit_time=datetime.now(timezone.utc).isoformat(),
                    pnl_usd=pnl,
                    exit_reason=reason,
                )
                log.info(f"[llm] Outcome recorded for decision {d['id']}: PnL={pnl:+.4f}")
                break
    except Exception as exc:
        log.warning(f"[llm] Could not record outcome: {exc}")


# ── Stop-loss check ───────────────────────────────────────────────────────────

def _check_stop_loss(
    exchange: ccxt.binance, symbol: str, price: float, state: dict,
    circuit: "DailyCircuitBreaker | None" = None,
) -> None:
    pos = state.get(symbol)
    if not pos:
        return
    if pos["side"] == "long" and price <= pos["stop_loss"]:
        log.warning(
            f"[{symbol}] STOP-LOSS hit (long)  price={price:.4f}  SL={pos['stop_loss']:.4f}"
        )
        _close_long(exchange, symbol, state, reason="stop-loss", circuit=circuit)
    elif pos["side"] == "short" and price >= pos["stop_loss"]:
        log.warning(
            f"[{symbol}] STOP-LOSS hit (short)  price={price:.4f}  SL={pos['stop_loss']:.4f}"
        )
        _close_short(exchange, symbol, state, reason="stop-loss", circuit=circuit)


# ── News cache ────────────────────────────────────────────────────────────────

class NewsCache:
    """
    Fetches and caches news sentiment scores, refreshing every NEWS_REFRESH_HOURS.
    This avoids hammering RSS feeds on every 15-minute cycle.
    """

    def __init__(self):
        self._scores: dict[str, float] = {}
        self._fetched_at = None

    def get(self, symbols: list[str]) -> dict[str, float]:
        now     = datetime.now(timezone.utc)
        refresh = config.NEWS_REFRESH_HOURS * 3600
        stale   = (
            self._fetched_at is None or
            (now - self._fetched_at).total_seconds() >= refresh
        )
        if stale:
            try:
                self._scores     = news_analyzer.get_market_sentiment(symbols)
                self._fetched_at = now
                for sym, score in self._scores.items():
                    mood = _mood(score)
                    log.info(f"[news] {sym}  score={score:+.3f}  ({mood})")
            except Exception as exc:
                log.warning(f"[news] Fetch failed: {exc} — using last known scores.")
        return self._scores


def _mood(score: float) -> str:
    if score >=  config.NEWS_STRONG_BULL: return "STRONGLY BULLISH"
    if score >=  config.NEWS_WEAK_BULL:   return "BULLISH"
    if score >  -config.NEWS_WEAK_BULL:   return "neutral"
    if score >  -config.NEWS_STRONG_BULL: return "bearish"
    return "STRONGLY BEARISH"


# ── Combined decision engine ──────────────────────────────────────────────────

def _decide_action(technical_signals: dict, news_score: float, current_side: "str | None") -> tuple[str, str]:
    """
    Combine technical signals and news sentiment into a single trade action.

    Parameters
    ----------
    technical_signals : dict with boolean keys long_entry, long_exit, short_entry, short_exit
    news_score : float, sentiment from -1.0 to +1.0
    current_side : None (no position), 'long', or 'short'

    Returns
    -------
    (action, reason)
    action  : 'open_long' | 'close_long' | 'open_short' | 'close_short' | 'hold' | 'skip'
    reason  : human-readable string logged to explain the decision

    Decision matrix
    ---------------
    Long position open:
        strongly bearish news            → close_long  (news-driven early exit)
        technical long-exit signal       → close_long  (standard exit)
        otherwise                        → hold

    Short position open:
        strongly bullish news            → close_short (news-driven early exit)
        technical short-exit signal      → close_short (standard exit)
        otherwise                        → hold

    No position:
        Long entry:
            strongly bullish news            → open_long  (news alone)
            bullish news + tech long         → open_long  (double confirmation)
            neutral + tech long              → open_long  (technical-only)
            bullish but no tech signal       → skip
            bearish / strongly bearish       → skip

        Short entry (only when LONG_ONLY=False):
            strongly bearish news            → open_short (news alone)
            bearish news + tech short        → open_short (double confirmation)
            neutral + tech short             → open_short (technical-only)
            bearish but no tech signal       → skip
            bullish / strongly bullish       → skip

        Otherwise                            → skip
    """
    strong_bull = news_score >=  config.NEWS_STRONG_BULL
    weak_bull   = news_score >=  config.NEWS_WEAK_BULL
    neutral     = news_score >  config.NEWS_WEAK_BEAR and news_score < config.NEWS_WEAK_BULL
    weak_bear   = news_score <=  config.NEWS_WEAK_BEAR
    strong_bear = news_score <=  config.NEWS_STRONG_BEAR

    tech_long        = technical_signals.get("long_entry", False)
    tech_long_exit   = technical_signals.get("long_exit", False)
    tech_short       = technical_signals.get("short_entry", False)
    tech_short_exit  = technical_signals.get("short_exit", False)

    # ── Manage open long ──────────────────────────────────────────────────────
    if current_side == "long":
        if strong_bear:
            return "close_long", f"news strongly bearish ({news_score:+.3f}) — exiting early"
        if tech_long_exit:
            return "close_long", f"technical exit signal (news={news_score:+.3f})"
        return "hold", f"holding long — news={_mood(news_score)}, no exit signal"

    # ── Manage open short ─────────────────────────────────────────────────────
    if current_side == "short":
        if strong_bull:
            return "close_short", f"news strongly bullish ({news_score:+.3f}) — exiting short early"
        if tech_short_exit:
            return "close_short", f"technical short-exit signal (news={news_score:+.3f})"
        return "hold", f"holding short — news={_mood(news_score)}, no exit signal"

    # ── No position — evaluate long entry ────────────────────────────────────
    if strong_bull:
        return "open_long", f"news STRONGLY BULLISH ({news_score:+.3f}) — entering long without technical"
    if weak_bull and tech_long:
        return "open_long", f"bullish news ({news_score:+.3f}) + technical long — double confirmation"
    if neutral and tech_long:
        return "open_long", f"neutral news ({news_score:+.3f}) — technical long signal"

    # ── No position — evaluate short entry (margin only) ─────────────────────
    if not config.LONG_ONLY:
        if strong_bear:
            return "open_short", f"news STRONGLY BEARISH ({news_score:+.3f}) — entering short without technical"
        if weak_bear and tech_short:
            return "open_short", f"bearish news ({news_score:+.3f}) + technical short — double confirmation"
        if neutral and tech_short:
            return "open_short", f"neutral news ({news_score:+.3f}) — technical short signal"

    if weak_bear or strong_bear:
        return "skip", f"news {'STRONGLY ' if strong_bear else ''}bearish ({news_score:+.3f}) — protecting capital"

    log.debug(
        f"skip — tech={technical_signals}, news={news_score:+.3f}, "
        f"strong_bull={strong_bull}, weak_bull={weak_bull}, "
        f"neutral={neutral}, weak_bear={weak_bear}, strong_bear={strong_bear}"
    )
    return "skip", "no clear edge"


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_once(
    exchange: ccxt.binance,
    symbols: list,
    state: dict,
    news_cache: NewsCache,
    circuit: DailyCircuitBreaker,
    brain_mode: str = "shadow",
) -> None:
    """Evaluate all symbols once and act on signals."""

    # Refresh circuit breaker day boundary and feed current balance
    try:
        current_balance = _get_usdt_balance(exchange)
        circuit.reset_if_new_day(current_balance)
    except Exception as exc:
        log.warning(f"Could not fetch balance for circuit breaker: {exc}")

    if circuit.halted:
        log.warning("[circuit] Trading halted for today — skipping cycle.")
        return

    news_scores = news_cache.get(symbols)

    for symbol in symbols:
        try:
            technical_signals, price = _get_signal(exchange, symbol)
            news_score = news_scores.get(symbol, 0.0)

            # Always check stop-loss first (price-based, not signal-based)
            _check_stop_loss(exchange, symbol, price, state, circuit=circuit)

            # Re-check circuit after potential stop-loss close
            if circuit.halted:
                log.warning(f"[{symbol}] Circuit breaker triggered — skipping remaining symbols.")
                return

            pos          = state.get(symbol)
            current_side = pos["side"] if pos else None
            action, reason = _decide_action(technical_signals, news_score, current_side)

            log.info(
                f"[{symbol}]  price={price:.4f}  tech={technical_signals}  "
                f"news={news_score:+.3f}  pos={current_side or 'none'}  → {action.upper()}  ({reason})"
            )

            if action == "open_long":
                _open_long(exchange, symbol, price, state)
            elif action == "close_long":
                _close_long(exchange, symbol, state, reason=reason, circuit=circuit)
            elif action == "open_short":
                _open_short(exchange, symbol, price, state)
            elif action == "close_short":
                _close_short(exchange, symbol, state, reason=reason, circuit=circuit)

            # ── LLM shadow mode ──────────────────────────────────────────────
            # After the rule engine acts, run the LLM brain and journal its
            # decision (but do NOT execute it).
            if brain_mode in ("shadow", "llm"):
                _run_llm_shadow(
                    exchange, symbol, state, news_scores,
                    circuit, current_balance,
                )

        except Exception as exc:
            log.error(f"[{symbol}] Error in evaluation cycle: {exc}", exc_info=True)


def _run_llm_shadow(
    exchange: ccxt.binance, symbol: str, state: dict,
    news_scores: dict, circuit: DailyCircuitBreaker, balance: float,
) -> None:
    """Run the LLM decision engine and journal the result (shadow mode)."""
    try:
        frames = {
            config.BASE_TF:   _fetch_recent_bars(exchange, symbol, config.BASE_TF),
            config.TREND_TF1: _fetch_recent_bars(exchange, symbol, config.TREND_TF1),
            config.TREND_TF2: _fetch_recent_bars(exchange, symbol, config.TREND_TF2),
        }
        df = indicators.build(frames)

        llm_decision = llm_trader.make_decision(
            df=df,
            symbol=symbol,
            balance=balance,
            state=state if state else None,
            news_cache=news_scores,
            circuit_daily_pnl=circuit._daily_pnl,
            circuit_start_balance=circuit._start_balance,
            circuit_halted=circuit.halted,
        )
        log.info(
            f"[llm-shadow] {symbol}: {llm_decision['action']}  "
            f"conf={llm_decision.get('confidence', 0):.2f}  "
            f"id={llm_decision.get('decision_id', 0)}"
        )
    except Exception as exc:
        log.warning(f"[llm-shadow] {symbol} LLM call failed: {exc}")


# ── Startup position sync ────────────────────────────────────────────────────

def _sync_positions_from_exchange(exchange: ccxt.binance, symbols: list, state: dict) -> None:
    """
    On startup, reconcile local state with the margin account on the exchange.
    Margin trading has no "positions" endpoint — we infer position state from balances:

    - Long position:  we hold base asset (e.g. BTC total > dust)
    - Short position: we have borrowed base asset (e.g. BTC debt > dust)

    If local state claims a position but the exchange balance doesn't confirm it,
    the stale entry is removed.
    """
    DUST = 1e-6  # minimum meaningful quantity

    try:
        balance = exchange.fetch_balance()
    except Exception as exc:
        log.warning(f"Could not fetch balance for position sync: {exc} — using local state as-is.")
        return

    changed = False

    for sym in list(state.keys()):
        # Parse base asset, e.g. "BTC" from "BTC/USDT"
        base = sym.split("/")[0] if "/" in sym else sym
        asset = balance.get(base) or {}
        total = float(asset.get("total") or 0)
        debt  = float(asset.get("debt") or asset.get("borrowed") or 0)

        pos  = state[sym]
        side = pos.get("side")

        if side == "long" and total < DUST:
            log.warning(f"[{sym}] State says LONG but no {base} balance found — removing stale entry.")
            del state[sym]
            changed = True
        elif side == "short" and debt < DUST:
            log.warning(f"[{sym}] State says SHORT but no {base} debt found — removing stale entry.")
            del state[sym]
            changed = True
        else:
            log.info(f"[{sym}] Position confirmed: {side}  {base} total={total:.8f}  debt={debt:.8f}")

    if changed:
        _save_state(state)

    # Adopt orphaned exchange positions not in local state
    for sym in symbols:
        if sym in state:
            continue
        base = sym.split("/")[0] if "/" in sym else sym
        asset = balance.get(base) or {}
        total = float(asset.get("total") or 0)
        debt  = float(asset.get("debt") or asset.get("borrowed") or 0)

        if total > DUST and debt < DUST:
            # Have base asset, no debt → orphaned long
            log.warning(f"[{sym}] Orphaned LONG detected on exchange ({base} total={total:.8f}) — adopting.")
            try:
                price = _fetch_current_price(exchange, sym)
                state[sym] = {
                    "side":        "long",
                    "entry_price": price,
                    "qty":         total,
                    "stop_loss":   round(price * (1 - config.STOP_LOSS_PCT), 8),
                    "entry_time":  datetime.now(timezone.utc).isoformat(),
                    "notional":    total * price,
                    "order_id":    None,
                    "sl_order_id": None,
                    "adopted":     True,
                }
                changed = True
            except Exception as exc:
                log.warning(f"[{sym}] Could not adopt orphaned position: {exc}")

        elif debt > DUST:
            # Have base debt → orphaned short
            log.warning(f"[{sym}] Orphaned SHORT detected on exchange ({base} debt={debt:.8f}) — adopting.")
            try:
                price = _fetch_current_price(exchange, sym)
                state[sym] = {
                    "side":        "short",
                    "entry_price": price,
                    "qty":         debt,
                    "stop_loss":   round(price * (1 + config.STOP_LOSS_PCT), 8),
                    "entry_time":  datetime.now(timezone.utc).isoformat(),
                    "notional":    debt * price,
                    "order_id":    None,
                    "sl_order_id": None,
                    "adopted":     True,
                }
                changed = True
            except Exception as exc:
                log.warning(f"[{sym}] Could not adopt orphaned position: {exc}")

    if changed:
        _save_state(state)
    else:
        log.info("Position sync: local state matches exchange.")


# ── Real-time price + SL check ────────────────────────────────────────────────

def _fetch_current_price(exchange: ccxt.binance, symbol: str) -> float:
    """Lightweight ticker fetch — no candles, just last price."""
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker.get("last") or ticker.get("close"))


def _realtime_sl_check(
    exchange: ccxt.binance, symbols: list, state: dict, circuit: DailyCircuitBreaker
) -> None:
    """Check stop-losses against live price for all open positions."""
    for symbol in symbols:
        if symbol not in state:
            continue
        try:
            price = _fetch_current_price(exchange, symbol)
            _check_stop_loss(exchange, symbol, price, state, circuit=circuit)
        except Exception as exc:
            log.warning(f"[{symbol}] Real-time SL check failed: {exc}")


# ── Scheduling ────────────────────────────────────────────────────────────────

def _seconds_to_next_bar(tf: str, buffer_secs: int = 10) -> float:
    """
    Seconds until the next bar boundary + buffer.
    e.g. for 15m: fires at :00, :15, :30, :45 + 10s
    """
    period = TF_SECONDS.get(tf, 900)
    now    = datetime.now(timezone.utc)
    ts     = now.timestamp()
    elapsed_in_bar = ts % period
    remaining      = period - elapsed_in_bar + buffer_secs
    return remaining


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Binance live trading bot")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--live", action="store_true", help="Use real Binance (NOT testnet)")
    parser.add_argument(
        "--brain", default="shadow",
        choices=["shadow", "rules", "llm"],
        help="LLM brain mode: shadow (journal only, default), rules (off), llm (live, Phase 3)",
    )
    args = parser.parse_args()

    _load_best_params()

    brain_mode = args.brain
    testnet     = not args.live
    exchange    = _connect(testnet=testnet)
    state       = _load_state()
    symbols     = config.CRYPTO_SYMBOLS
    news_cache  = NewsCache()
    circuit     = DailyCircuitBreaker()

    # Sync open positions from exchange to avoid double-entries after restart
    _sync_positions_from_exchange(exchange, symbols, state)

    if args.live:
        load_dotenv()
        if os.getenv("CONFIRM_LIVE_TRADING") != "YES":
            log.error(
                "Live mode requires env var CONFIRM_LIVE_TRADING=YES.\n"
                "Set it in your .env file or systemd service to confirm you accept the risks."
            )
            sys.exit(1)
        log.warning("=" * 60)
        log.warning("  LIVE MODE — REAL MONEY WILL BE TRADED")
        log.warning("=" * 60)

    mode_str = "LIVE" if args.live else "TESTNET"
    log.info(f"Bot started  mode={mode_str}  symbols={symbols}  tf={config.BASE_TF}  brain={brain_mode}")
    log.info(
        f"Params  BB({config.BB_PERIOD},{config.BB_STD})  "
        f"RSI({config.RSI_PERIOD})  SL={config.STOP_LOSS_PCT*100:.1f}%  "
        f"Risk={config.RISK_PER_TRADE*100:.0f}%/trade  "
        f"MaxDailyLoss={config.MAX_DAILY_LOSS_PCT*100:.0f}%  "
        f"MarginType={config.MARGIN_TYPE}  "
        f"LongOnly={config.LONG_ONLY}"
    )

    if args.once:
        run_once(exchange, symbols, state, news_cache, circuit, brain_mode=brain_mode)
        return

    log.info(f"Running continuously — signals every {config.BASE_TF} bar, SL checked every 60s.")

    # Track params file modification time for hot reload
    _params_mtime = PARAMS_FILE.stat().st_mtime if PARAMS_FILE.exists() else 0
    # -1 ensures a full run fires immediately on first iteration
    _last_full_run_bar: int = -1
    _period = TF_SECONDS.get(config.BASE_TF, 900)

    while True:
        # Hot reload: pick up new best_params.json without restarting.
        if PARAMS_FILE.exists():
            try:
                mtime = PARAMS_FILE.stat().st_mtime
                if mtime > _params_mtime:
                    log.info("best_params.json updated — hot-reloading parameters.")
                    _load_best_params()
                    _params_mtime = mtime
            except OSError:
                pass

        # Determine the most recent closed bar (10s buffer after bar boundary)
        now_ts = time.time()
        current_bar_ts = int((now_ts - 10) // _period) * _period

        if current_bar_ts != _last_full_run_bar:
            # New bar closed — run full signal + entry/exit cycle
            try:
                run_once(exchange, symbols, state, news_cache, circuit, brain_mode=brain_mode)
            except Exception as exc:
                log.error(f"Unexpected error in main loop: {exc}", exc_info=True)
            _last_full_run_bar = current_bar_ts
            secs_to_next = _period - (time.time() % _period) + 10
            log.info(
                f"Next bar in ~{secs_to_next:.0f}s  "
                f"(current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC)"
            )
        else:
            # Between bars — check stop-losses against live price every 60s
            open_positions = [s for s in symbols if s in state]
            if open_positions:
                log.info(f"[realtime-SL] Checking {len(open_positions)} open position(s)...")
                _realtime_sl_check(exchange, symbols, state, circuit)

        time.sleep(60)


if __name__ == "__main__":
    main()
