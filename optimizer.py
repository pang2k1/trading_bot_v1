"""
optimizer.py
────────────
Bayesian hyperparameter optimizer using Optuna.

Tunes indicator and risk parameters to maximise risk-adjusted returns
on recent historical data. Results are saved to best_params.json and
automatically picked up by live_trader.py on next start.

How it works
------------
1. Fetches 6 months of candle data from Binance (free, no API key needed)
2. Runs hundreds of backtests, each with a different set of parameters
3. Uses Bayesian optimization (Optuna TPE sampler) to intelligently explore
   the parameter space — much faster than a brute-force grid search
4. Scores each trial on: Sharpe ratio − drawdown penalty
5. Saves the best parameter set to best_params.json

Usage
-----
    python optimizer.py                          # 100 trials on BTC/USDT
    python optimizer.py --trials 300             # more trials = better results
    python optimizer.py --symbol ETH/USDT        # optimize for ETH
    python optimizer.py --trials 200 --apply     # optimize and save params
    python optimizer.py --schedule               # re-optimize every 7 days (continuous)

Parameters tuned
----------------
    BB_PERIOD, BB_STD
    RSI_PERIOD, RSI_LONG_ENTRY, RSI_SHORT_ENTRY, RSI_LONG_EXIT, RSI_SHORT_EXIT
    EMA_TREND1, EMA_TREND2
    STOP_LOSS_PCT
"""

import argparse
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import optuna
from tabulate import tabulate

import backtest
import config
import data_fetcher
import indicators
import strategy

log = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)

PARAMS_FILE  = Path("best_params.json")
RESULTS_FILE = Path("optimization_results.json")


# ── Config patching ───────────────────────────────────────────────────────────

# Only these params may be patched during optimization trials.
_TUNABLE_PARAMS = frozenset({
    "BB_PERIOD", "BB_STD",
    "RSI_PERIOD", "RSI_LONG_ENTRY", "RSI_SHORT_ENTRY", "RSI_LONG_EXIT", "RSI_SHORT_EXIT",
    "EMA_TREND1", "EMA_TREND2",
    "STOP_LOSS_PCT", "RISK_PER_TRADE",
    "START_DATE",  # allowed so _fetch_data can set the lookback window
})


@contextmanager
def _patched_config(params: dict):
    """Temporarily apply params dict to config, restore on exit."""
    filtered = {k: v for k, v in params.items() if k in _TUNABLE_PARAMS}
    original = {k: getattr(config, k) for k in filtered if hasattr(config, k)}
    for k, v in filtered.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in original.items():
            setattr(config, k, v)


# ── Backtest wrapper ──────────────────────────────────────────────────────────

def _run_backtest(frames: dict, params: dict) -> dict:
    """Run full indicator + strategy + backtest pipeline with patched params."""
    with _patched_config(params):
        df      = indicators.build(frames)
        df      = strategy.generate_signals(df)
        metrics, _ = backtest.run(df)
    return metrics


# ── Optuna objective ──────────────────────────────────────────────────────────

def _objective(trial: optuna.Trial, frames: dict) -> float:
    """
    Suggest parameters, run backtest, return optimisation score.

    Score = Sharpe ratio − 0.3 × |max_drawdown_pct| / 10
    Trials with fewer than 10 trades are pruned.
    """
    params = {
        "BB_PERIOD":       trial.suggest_int(  "BB_PERIOD",        10,  60),
        "BB_STD":          trial.suggest_float("BB_STD",            1.5,  3.5),
        "RSI_PERIOD":      trial.suggest_int(  "RSI_PERIOD",        7,   21),
        "RSI_LONG_ENTRY":  trial.suggest_int(  "RSI_LONG_ENTRY",   20,   48),
        "RSI_SHORT_ENTRY": trial.suggest_int(  "RSI_SHORT_ENTRY",  52,   80),
        "RSI_LONG_EXIT":   trial.suggest_int(  "RSI_LONG_EXIT",    50,   72),
        "RSI_SHORT_EXIT":  trial.suggest_int(  "RSI_SHORT_EXIT",   28,   50),
        "EMA_TREND1":      trial.suggest_int(  "EMA_TREND1",       10,   50),
        "EMA_TREND2":      trial.suggest_int(  "EMA_TREND2",       30,  150),
        "STOP_LOSS_PCT":   trial.suggest_float("STOP_LOSS_PCT",   0.005, 0.04),
    }

    # Enforce logical constraints — prune invalid combos early
    if params["RSI_LONG_ENTRY"]  >= params["RSI_LONG_EXIT"]:
        raise optuna.exceptions.TrialPruned()
    if params["RSI_SHORT_EXIT"]  >= params["RSI_SHORT_ENTRY"]:
        raise optuna.exceptions.TrialPruned()
    if params["EMA_TREND1"]      >= params["EMA_TREND2"]:
        raise optuna.exceptions.TrialPruned()

    metrics = _run_backtest(frames, params)

    if metrics["num_trades"] < 10:
        raise optuna.exceptions.TrialPruned()

    # Report intermediate score so the pruner can actually prune
    sharpe   = metrics["sharpe_ratio"]
    drawdown = abs(metrics["max_drawdown_pct"])
    score    = sharpe - 0.3 * (drawdown / 10)
    trial.report(score, step=0)

    return score


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_data(symbol: str, lookback_days: int = 180) -> dict:
    """Fetch recent candle data for optimization (avoids very old data)."""
    start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    with _patched_config({"START_DATE": start}):
        frames = data_fetcher.fetch_all_timeframes(symbol)
    return frames


# ── Optimization run ──────────────────────────────────────────────────────────

def _split_frames(frames: dict, train_ratio: float = 0.7) -> tuple[dict, dict]:
    """Split frames into train/test by time index."""
    split_point = int(len(frames[config.BASE_TF]) * train_ratio)
    cutoff = frames[config.BASE_TF].index[split_point]

    train_frames = {}
    test_frames = {}
    for tf, df in frames.items():
        train_frames[tf] = df.loc[:cutoff]
        test_frames[tf] = df.loc[cutoff:]
    return train_frames, test_frames


def optimize(symbol: str = "BTC/USDT", n_trials: int = 100) -> dict:
    """
    Run Optuna optimization with walk-forward validation.
    Optimizes on 70% train split, validates on 30% test split.
    Returns the best params dict.
    """
    print(f"\n{'='*60}")
    print(f"  Optimizer  —  {symbol}  —  {n_trials} trials")
    print(f"{'='*60}")
    print("Fetching data (last 6 months)...")

    frames = _fetch_data(symbol)
    base_df = frames[config.BASE_TF]
    print(
        f"  {config.BASE_TF}: {len(base_df)} bars  "
        f"({base_df.index[0].date()} → {base_df.index[-1].date()})"
    )

    # Walk-forward split: 70% train / 30% test
    train_frames, test_frames = _split_frames(frames, train_ratio=0.7)
    train_len = len(train_frames[config.BASE_TF])
    test_len = len(test_frames[config.BASE_TF])
    print(f"  Walk-forward split: train={train_len} bars, test={test_len} bars")

    # Baseline: run backtest with current (default) config params
    baseline = _run_backtest(frames, {})
    print(
        f"\nBaseline (current params):  "
        f"Sharpe={baseline['sharpe_ratio']:.2f}  "
        f"Return={baseline['total_return_pct']:.1f}%  "
        f"MaxDD={baseline['max_drawdown_pct']:.1f}%  "
        f"Trades={baseline['num_trades']}"
    )

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    study.optimize(
        lambda trial: _objective(trial, train_frames),
        n_trials=n_trials,
        show_progress_bar=True,
    )

    # Check if any trial completed successfully
    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    if not completed:
        print("\nAll trials were pruned — no valid parameter set found.")
        print("Try relaxing constraints or increasing the data range.")
        return {}

    best_params = study.best_params
    best_score  = study.best_value

    # Evaluate on both train and test splits
    train_metrics = _run_backtest(train_frames, best_params)
    test_metrics  = _run_backtest(test_frames, best_params)
    full_metrics  = _run_backtest(frames, best_params)

    print(f"\n{'='*60}")
    print(f"  Optimization complete  —  best train score: {best_score:.4f}")
    print(f"{'='*60}")

    # Overfitting check
    train_score = train_metrics["sharpe_ratio"]
    test_score  = test_metrics["sharpe_ratio"]
    if train_score > 0 and test_score < train_score * 0.3:
        print(
            f"\n  ⚠ WARNING: Test Sharpe ({test_score:.2f}) is much worse than "
            f"train ({train_score:.2f}) — possible overfitting."
        )
    print(f"\n  Train Sharpe: {train_score:.2f}  |  Test Sharpe: {test_score:.2f}")

    print("\nParameter comparison:")
    rows = []
    for k, v in best_params.items():
        default = getattr(config, k, "—")
        rows.append([k, default, v, "↑" if v != default else "="])
    print(tabulate(rows, headers=["Parameter", "Default", "Optimized", ""], tablefmt="simple"))

    print("\nBacktest results (optimized vs baseline):")
    compare_keys = [
        "total_return_pct", "sharpe_ratio", "win_rate_pct",
        "profit_factor", "max_drawdown_pct", "num_trades",
    ]
    rows = []
    for k in compare_keys:
        rows.append([
            k.replace("_", " ").title(),
            f"{baseline[k]:.2f}",
            f"{full_metrics[k]:.2f}",
        ])
    print(tabulate(rows, headers=["Metric", "Baseline", "Optimized"], tablefmt="simple"))

    # Save results history
    _save_results(symbol, best_params, full_metrics, best_score)

    return best_params


def _save_results(symbol: str, params: dict, metrics: dict, score: float) -> None:
    """Append optimization run to results history file."""
    history = []
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE) as f:
            history = json.load(f)
    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol":    symbol,
        "score":     round(score, 4),
        "params":    params,
        "metrics":   metrics,
    })
    with open(RESULTS_FILE, "w") as f:
        json.dump(history, f, indent=2)


def save_params(params: dict) -> None:
    with open(PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=2)
    print(f"\nBest params saved to {PARAMS_FILE}")
    print("The live trader will automatically use these on next start.")


# ── Live trade degradation detection ─────────────────────────────────────────

TRADES_LOG = Path("trades_log.csv")

def _analyze_live_trades(lookback: int = 10) -> dict:
    """
    Read trades_log.csv and check if recent performance is degrading.

    Parameters
    ----------
    lookback : number of recent trades to evaluate

    Returns
    -------
    dict with keys:
        num_trades  : total trades in log
        recent_n    : number of recent trades evaluated
        win_rate    : win rate of last `lookback` trades (0.0–1.0)
        avg_pnl     : average PnL of last `lookback` trades (USDT)
        degraded    : True if win_rate < 0.40 or avg_pnl < 0
    """
    if not TRADES_LOG.exists():
        return {"num_trades": 0, "recent_n": 0, "win_rate": None, "avg_pnl": None, "degraded": False}

    try:
        df = pd.read_csv(TRADES_LOG)
    except Exception as exc:
        log.warning(f"Could not read trades_log.csv: {exc}")
        return {"num_trades": 0, "recent_n": 0, "win_rate": None, "avg_pnl": None, "degraded": False}

    if df.empty or "pnl_usd" not in df.columns:
        return {"num_trades": 0, "recent_n": 0, "win_rate": None, "avg_pnl": None, "degraded": False}

    recent   = df.tail(lookback)
    win_rate = (recent["pnl_usd"] > 0).mean()
    avg_pnl  = recent["pnl_usd"].mean()
    degraded = win_rate < 0.40 or avg_pnl < 0

    return {
        "num_trades": len(df),
        "recent_n":   len(recent),
        "win_rate":   round(float(win_rate), 3),
        "avg_pnl":    round(float(avg_pnl), 4),
        "degraded":   degraded,
    }


# ── Scheduled re-optimization ─────────────────────────────────────────────────

def run_scheduled(symbol: str, n_trials: int, interval_days: int = 7) -> None:
    """
    Re-optimize every `interval_days` days and save params automatically.
    Also triggers early re-optimization if live trade performance degrades
    (win rate < 40% or avg PnL < 0 over the last 10 trades).
    Runs indefinitely — use alongside live_trader.py.
    """
    print(f"Scheduled optimizer started — will re-tune every {interval_days} days.")
    CHECK_INTERVAL = 3600  # check degradation every hour
    next_full_run  = time.time()  # run immediately on first start

    while True:
        now = time.time()

        # Check if a scheduled full run is due
        if now >= next_full_run:
            try:
                best = optimize(symbol=symbol, n_trials=n_trials)
                save_params(best)
                print(f"Params updated. Next scheduled optimization in {interval_days} days.")
            except Exception as exc:
                log.error(f"Optimization cycle failed: {exc}", exc_info=True)
            next_full_run = time.time() + interval_days * 86400
            time.sleep(CHECK_INTERVAL)
            continue

        # Check for performance degradation between scheduled runs
        analysis = _analyze_live_trades(lookback=10)
        if analysis["num_trades"] >= 10 and analysis["degraded"]:
            print(
                f"[degradation] win_rate={analysis['win_rate']:.1%}  "
                f"avg_pnl={analysis['avg_pnl']:+.4f} USDT  "
                f"— triggering early re-optimization."
            )
            try:
                best = optimize(symbol=symbol, n_trials=n_trials)
                save_params(best)
                print("Early re-optimization complete. Resuming normal schedule.")
            except Exception as exc:
                log.error(f"Early optimization failed: {exc}", exc_info=True)
            # Reset scheduled timer so we don't immediately re-run
            next_full_run = time.time() + interval_days * 86400
        else:
            if analysis["num_trades"] > 0:
                print(
                    f"[health check] trades={analysis['num_trades']}  "
                    f"win_rate={analysis['win_rate']:.1%}  "
                    f"avg_pnl={analysis['avg_pnl']:+.4f} USDT  — OK"
                )

        time.sleep(CHECK_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy parameter optimizer")
    parser.add_argument("--trials",   type=int,   default=100,       help="Number of Optuna trials")
    parser.add_argument("--symbol",   type=str,   default="BTC/USDT", help="Symbol to optimize on")
    parser.add_argument("--apply",    action="store_true",            help="Save best params to best_params.json")
    parser.add_argument("--schedule", action="store_true",            help="Re-optimize every 7 days continuously")
    parser.add_argument("--days",     type=int,   default=7,          help="Interval (days) for --schedule")
    args = parser.parse_args()

    if args.schedule:
        run_scheduled(symbol=args.symbol, n_trials=args.trials, interval_days=args.days)
        return

    best = optimize(symbol=args.symbol, n_trials=args.trials)

    if args.apply:
        save_params(best)
    else:
        print(f"\nRun with --apply to save these params to {PARAMS_FILE}")
        print("The live trader will automatically load best_params.json on start.")


if __name__ == "__main__":
    main()
