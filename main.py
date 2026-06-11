"""
main.py
Multi-Timeframe BB Mean Reversion backtest for crypto.

Usage
-----
    python main.py
"""

from tabulate import tabulate

import config
import data_fetcher
import indicators
import strategy
import backtest


def run_symbol(symbol: str) -> dict | None:
    sep = "=" * 66
    print(f"\n{sep}")
    print(f"  {symbol}  |  {config.TREND_TF2} trend -> {config.TREND_TF1} trend -> {config.BASE_TF} entry")
    print(sep)

    frames = data_fetcher.fetch_all_timeframes(symbol)

    for tf, df in frames.items():
        print(f"    {tf:>4s} : {len(df):,} bars  ({df.index[0].date()} to {df.index[-1].date()})")

    df = indicators.build(frames)
    print(f"    Combined base bars (after warm-up drop): {len(df):,}")

    df = strategy.generate_signals(df)

    n_long_entry  = df["long_entry"].sum()
    n_short_entry = df["short_entry"].sum()
    print(f"    Signals generated -> LONG: {n_long_entry}  SHORT: {n_short_entry}")

    metrics, trades = backtest.run(df)

    # Metrics table
    rows = [(k.replace("_", " ").title(), v) for k, v in metrics.items()]
    print()
    print(tabulate(rows, headers=["Metric", "Value"], tablefmt="simple",
                   floatfmt=".2f"))

    # Trade breakdown
    if not trades.empty:
        wins   = trades[trades["pnl_usd"] > 0]
        losses = trades[trades["pnl_usd"] <= 0]
        print(f"\n  Wins: {len(wins)}  |  Losses: {len(losses)}")
        print(f"  Best trade : +{trades['pnl_pct'].max():.2f}%")
        print(f"  Worst trade:  {trades['pnl_pct'].min():.2f}%")

        print(f"\n  Last 10 trades:")
        cols = ["side", "entry_time", "exit_time", "entry_price", "exit_price", "pnl_usd", "pnl_pct"]
        if "note" in trades.columns:
            cols.append("note")
        show = trades[cols].tail(10).copy()
        show["entry_time"] = show["entry_time"].dt.strftime("%Y-%m-%d %H:%M")
        show["exit_time"]  = show["exit_time"].dt.strftime("%Y-%m-%d %H:%M")
        print(tabulate(show, headers="keys", tablefmt="simple",
                       floatfmt=".4f", showindex=False))

    return {"symbol": symbol, **metrics}


def main() -> None:
    print("\nStrategy : Multi-TF BB Mean Reversion + RSI Confluence")
    print(f"Timeframes: {config.TREND_TF2} (macro) -> {config.TREND_TF1} (trend) -> {config.BASE_TF} (entry)")
    print(f"Period    : {config.START_DATE} to today")
    print(f"Params    : BB({config.BB_PERIOD},{config.BB_STD})  RSI({config.RSI_PERIOD})"
          f"  EMA1h({config.EMA_TREND1})  EMA4h({config.EMA_TREND2})"
          f"  SL={config.STOP_LOSS_PCT*100:.1f}%  Risk={config.RISK_PER_TRADE*100:.0f}%/trade")

    summaries = []
    for symbol in config.CRYPTO_SYMBOLS:
        try:
            result = run_symbol(symbol)
            if result:
                summaries.append(result)
        except Exception as exc:
            import traceback
            print(f"\n  [ERROR] {symbol}: {exc}")
            traceback.print_exc()

    if summaries:
        print(f"\n\n{'=' * 66}")
        print("  SUMMARY")
        print("=" * 66)
        rows = []
        for s in summaries:
            rows.append([
                s["symbol"],
                s["num_trades"],
                f"{s['win_rate_pct']:.1f}%",
                f"{s['profit_factor']:.2f}",
                f"{s['total_return_pct']:.1f}%",
                f"${s['final_equity']:,.2f}",
                f"{s['max_drawdown_pct']:.2f}%",
                f"{s['sharpe_ratio']:.2f}",
                f"{s['avg_win_pct']:.3f}%",
                f"{s['avg_loss_pct']:.3f}%",
                s["stop_loss_hits"],
                s["long_trades"],
                s["short_trades"],
            ])
        print(tabulate(
            rows,
            headers=["Symbol", "Trades", "Win Rate", "PF", "Return",
                     "Final $", "Max DD", "Sharpe", "Avg Win", "Avg Loss",
                     "SL Hits", "Longs", "Shorts"],
            tablefmt="simple",
        ))

    print("\nDone.")


if __name__ == "__main__":
    main()
