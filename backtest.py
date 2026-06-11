"""
backtest.py
Event-driven backtesting engine.
Supports long and short positions (when config.LONG_ONLY is False),
stop-loss, slippage, and fixed-fractional sizing.

Signal columns (booleans from strategy.py)
-------------------------------------------
long_entry  = open long
long_exit   = close long
short_entry = open short
short_exit  = close short
"""

import numpy as np
import pandas as pd

import config


def run(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    equity   = config.INITIAL_CAPITAL
    position = None
    trades   = []

    # Extract arrays for fast iteration (avoid iterrows overhead)
    closes      = df["close"].values.astype(float)
    lows        = df["low"].values.astype(float)
    highs       = df["high"].values.astype(float)
    long_entry  = df["long_entry"].values.astype(bool)
    long_exit   = df["long_exit"].values.astype(bool)
    short_entry = df["short_entry"].values.astype(bool)
    short_exit  = df["short_exit"].values.astype(bool)
    timestamps  = df.index
    long_only   = config.LONG_ONLY
    slippage    = config.SLIPPAGE_PCT

    for i in range(len(closes)):
        price    = closes[i]
        bar_low  = lows[i]
        bar_high = highs[i]
        ts       = timestamps[i]

        # ── Check stop-loss / exit on open position ────────────────────────
        if position is not None:
            side     = position["side"]
            stop_hit = (
                (side == "long"  and bar_low <= position["stop_loss"]) or
                (side == "short" and bar_high >= position["stop_loss"])
            )
            exit_signal = (
                (side == "long"  and long_exit[i]) or
                (side == "short" and short_exit[i])
            )

            if stop_hit:
                exit_px = position["stop_loss"]
                if side == "long":
                    exit_px *= (1 - slippage)
                else:
                    exit_px *= (1 + slippage)
                pnl, equity = _close(position, exit_px, equity, exit_ts=ts)
                trades.append(_record(position, exit_px, ts, pnl, "stop-loss"))
                position = None
            elif exit_signal:
                exit_px = price
                if side == "long":
                    exit_px *= (1 - slippage)
                else:
                    exit_px *= (1 + slippage)
                pnl, equity = _close(position, exit_px, equity, exit_ts=ts)
                trades.append(_record(position, exit_px, ts, pnl, ""))
                position = None

        # ── Open new position ──────────────────────────────────────────────
        if position is None:
            if long_entry[i]:
                fill_px = price * (1 + slippage)
                position, equity = _open("long", fill_px, ts, equity)
            elif short_entry[i] and not long_only:
                fill_px = price * (1 - slippage)
                position, equity = _open("short", fill_px, ts, equity)

    # ── Force-close at last bar ────────────────────────────────────────────
    if position is not None:
        last_px = closes[-1]
        if position["side"] == "long":
            last_px *= (1 - slippage)
        else:
            last_px *= (1 + slippage)
        pnl, equity = _close(position, last_px, equity, exit_ts=timestamps[-1])
        trades.append(_record(position, last_px, timestamps[-1], pnl, "forced close"))

    trades_df = pd.DataFrame(trades)
    return _metrics(trades_df, equity), trades_df


# ── Position helpers ──────────────────────────────────────────────────────────

_MIN_NOTIONAL = 5.5  # Binance minimum — keep in sync with live_trader


def _open(side: str, price: float, ts, equity: float):
    notional = max(equity * config.RISK_PER_TRADE, _MIN_NOTIONAL)
    qty      = notional / price
    comm     = notional * config.COMMISSION

    if side == "long":
        stop_loss = price * (1 - config.STOP_LOSS_PCT)
        equity   -= notional + comm
    else:
        stop_loss = price * (1 + config.STOP_LOSS_PCT)
        equity   -= comm

    return {
        "side"        : side,
        "entry_time"  : ts,
        "entry_price" : price,
        "qty"         : qty,
        "notional"    : notional,
        "entry_comm"  : comm,
        "stop_loss"   : stop_loss,
    }, equity


def _close(pos: dict, exit_price: float, equity: float, exit_ts=None):
    qty      = pos["qty"]
    exit_comm = qty * exit_price * config.COMMISSION

    if pos["side"] == "long":
        proceeds = qty * exit_price - exit_comm
        pnl      = proceeds - pos["notional"] - pos["entry_comm"]
        equity  += pos["notional"] + pos["entry_comm"] + pnl
    else:
        pnl = (pos["entry_price"] - exit_price) * qty - exit_comm - pos["entry_comm"]
        # Deduct margin borrow cost for shorts
        if exit_ts is not None and hasattr(pos["entry_time"], "timestamp"):
            hours_held = (exit_ts.timestamp() - pos["entry_time"].timestamp()) / 3600
            borrow_cost = pos["notional"] * config.MARGIN_INTEREST_HOURLY * max(hours_held, 0)
            pnl -= borrow_cost
        equity += pnl + pos["entry_comm"]

    return pnl, equity


def _record(pos: dict, exit_px: float, exit_ts, pnl: float, note: str) -> dict:
    notional = pos["notional"]
    r = {
        "side"        : pos["side"],
        "entry_time"  : pos["entry_time"],
        "exit_time"   : exit_ts,
        "entry_price" : pos["entry_price"],
        "exit_price"  : exit_px,
        "qty"         : pos["qty"],
        "pnl_usd"     : round(pnl, 4),
        "pnl_pct"     : round(pnl / notional * 100, 4),
    }
    if note:
        r["note"] = note
    return r


# ── Metrics ───────────────────────────────────────────────────────────────────

def _metrics(trades: pd.DataFrame, final_equity: float) -> dict:
    initial = config.INITIAL_CAPITAL
    ret_pct = (final_equity - initial) / initial * 100

    empty = {
        "initial_capital"  : initial,
        "final_equity"     : round(final_equity, 2),
        "total_return_pct" : 0.0,
        "num_trades"       : 0,
        "win_rate_pct"     : 0.0,
        "avg_win_pct"      : 0.0,
        "avg_loss_pct"     : 0.0,
        "profit_factor"    : 0.0,
        "max_drawdown_pct" : 0.0,
        "sharpe_ratio"     : 0.0,
        "long_trades"      : 0,
        "short_trades"     : 0,
        "stop_loss_hits"   : 0,
    }
    if trades.empty:
        return empty

    wins   = trades[trades["pnl_usd"] > 0]
    losses = trades[trades["pnl_usd"] <= 0]

    win_rate = len(wins) / len(trades) * 100
    avg_win  = wins["pnl_pct"].mean()   if not wins.empty   else 0.0
    avg_loss = losses["pnl_pct"].mean() if not losses.empty else 0.0

    gross_profit = wins["pnl_usd"].sum()
    gross_loss   = abs(losses["pnl_usd"].sum())
    if gross_loss > 0:
        profit_factor = min(gross_profit / gross_loss, 99.0)
    elif gross_profit > 0:
        profit_factor = 99.0
    else:
        profit_factor = 0.0

    # Max drawdown from running equity curve
    equity_curve = initial + trades["pnl_usd"].cumsum()
    running_peak = equity_curve.cummax()
    drawdowns = (equity_curve - running_peak) / running_peak * 100
    max_dd = drawdowns.min()

    # Sharpe: annualize daily returns with sqrt(365) for crypto (7-day markets)
    if "exit_time" in trades.columns and len(trades) > 1:
        daily_pnl = trades.set_index("exit_time")["pnl_usd"].resample("1D").sum()
        daily_ret = daily_pnl / initial
        if daily_ret.std() > 0:
            sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(365)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    stop_hits = int(trades.get("note", pd.Series(dtype=str))
                    .str.contains("stop", na=False).sum()) \
        if "note" in trades.columns else 0

    return {
        "initial_capital"  : initial,
        "final_equity"     : round(final_equity, 2),
        "total_return_pct" : round(ret_pct, 2),
        "num_trades"       : len(trades),
        "win_rate_pct"     : round(win_rate, 2),
        "avg_win_pct"      : round(avg_win, 4),
        "avg_loss_pct"     : round(avg_loss, 4),
        "profit_factor"    : round(profit_factor, 3),
        "max_drawdown_pct" : round(max_dd, 2),
        "sharpe_ratio"     : round(sharpe, 2),
        "long_trades"      : int((trades["side"] == "long").sum()),
        "short_trades"     : int((trades["side"] == "short").sum()),
        "stop_loss_hits"   : stop_hits,
    }
