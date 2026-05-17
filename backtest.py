"""
backtest.py
Event-driven backtesting engine.
Supports long and short positions (when config.LONG_ONLY is False),
stop-loss, slippage, and fixed-fractional sizing.

Signal conventions
------------------
 1  = open long
-1  = close long
-2  = open short
 2  = close short
"""

import numpy as np
import pandas as pd

import config


def run(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    equity   = config.INITIAL_CAPITAL
    position = None
    trades   = []

    for ts, row in df.iterrows():
        price  = float(row["close"])
        signal = int(row["signal"])

        # ── Check stop-loss / exit on open position ────────────────────────
        if position is not None:
            side     = position["side"]
            stop_hit = (
                (side == "long"  and price <= position["stop_loss"]) or
                (side == "short" and price >= position["stop_loss"])
            )
            exit_signal = (
                (side == "long"  and signal == -1) or
                (side == "short" and signal ==  2)
            )

            if stop_hit or exit_signal:
                exit_px = position["stop_loss"] if stop_hit else price
                # Apply slippage on exit (adverse direction)
                if side == "long":
                    exit_px *= (1 - config.SLIPPAGE_PCT)
                else:
                    exit_px *= (1 + config.SLIPPAGE_PCT)
                pnl, equity = _close(position, exit_px, equity)
                trades.append(_record(position, exit_px, ts, pnl,
                                      "stop-loss" if stop_hit else ""))
                position = None

        # ── Open new position ──────────────────────────────────────────────
        if position is None:
            if signal == 1:
                # Apply slippage on entry (adverse direction for long)
                fill_px = price * (1 + config.SLIPPAGE_PCT)
                position, equity = _open("long", fill_px, ts, equity)
            elif signal == -2 and not config.LONG_ONLY:
                # Apply slippage on entry (adverse direction for short)
                fill_px = price * (1 - config.SLIPPAGE_PCT)
                position, equity = _open("short", fill_px, ts, equity)

    # ── Force-close at last bar ────────────────────────────────────────────
    if position is not None:
        last_px = float(df["close"].iloc[-1])
        if position["side"] == "long":
            last_px *= (1 - config.SLIPPAGE_PCT)
        else:
            last_px *= (1 + config.SLIPPAGE_PCT)
        pnl, equity = _close(position, last_px, equity)
        trades.append(_record(position, last_px, df.index[-1], pnl, "forced close"))

    trades_df = pd.DataFrame(trades)
    return _metrics(trades_df, equity), trades_df


# ── Position helpers ──────────────────────────────────────────────────────────

def _open(side: str, price: float, ts, equity: float):
    notional = equity * config.RISK_PER_TRADE
    qty      = notional / price
    comm     = notional * config.COMMISSION

    if side == "long":
        stop_loss = price * (1 - config.STOP_LOSS_PCT)
        equity   -= notional + comm
    else:
        stop_loss = price * (1 + config.STOP_LOSS_PCT)
        equity   -= comm   # short: no cash locked, just commission upfront

    return {
        "side"        : side,
        "entry_time"  : ts,
        "entry_price" : price,
        "qty"         : qty,
        "notional"    : notional,
        "entry_comm"  : comm,
        "stop_loss"   : stop_loss,
    }, equity


def _close(pos: dict, exit_price: float, equity: float):
    qty      = pos["qty"]
    exit_comm = qty * exit_price * config.COMMISSION

    if pos["side"] == "long":
        proceeds = qty * exit_price - exit_comm
        pnl      = proceeds - pos["notional"] - pos["entry_comm"]
        equity  += pos["notional"] + pos["entry_comm"] + pnl
    else:
        pnl      = (pos["entry_price"] - exit_price) * qty - exit_comm - pos["entry_comm"]
        equity  += pos["notional"] + pnl

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
    # Cap profit_factor at 99.0 instead of infinity so JSON serialisation never breaks
    if gross_loss > 0:
        profit_factor = min(gross_profit / gross_loss, 99.0)
    elif gross_profit > 0:
        profit_factor = 99.0
    else:
        profit_factor = 0.0

    cum      = trades["pnl_usd"].cumsum()
    max_dd   = (cum - cum.cummax()).min() / initial * 100

    pct_std = trades["pnl_pct"].std()
    sharpe  = (trades["pnl_pct"].mean() / pct_std * np.sqrt(252)) if pct_std > 0 else 0.0

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
