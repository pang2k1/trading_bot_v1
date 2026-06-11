# Fix Checklist — trading_bot_v1

Ordered by priority. Each item lists the file, the problem, and the required fix.
Do P0 first, add the tests in P0.5, then continue. Don't change strategy logic/parameters beyond what's written here.

---

## P0 — Critical bugs (bot is currently broken or dangerous)

- [ ] **1. `strategy.py` — signal collision: no entry signal ever survives**
  - Problem: all four signals share one `signal` column; later `.loc` writes overwrite earlier ones. Every long-entry bar (`close <= bb_lower`) also satisfies short-exit (`close <= bb_mid`), so `+1` becomes `+2`. Every short-entry bar satisfies long-exit, so `-2` becomes `-1`. Net result: zero entries in backtests, optimizer prunes all trials.
  - Fix: replace the single int `signal` column with four boolean columns: `long_entry`, `long_exit`, `short_entry`, `short_exit`. Update all consumers:
    - `backtest.py` (`run`): read the boolean columns instead of int codes.
    - `live_trader.py` (`_get_signal`, `_decide_action`): return/consume the booleans (e.g. a small dataclass or dict), not `-2/-1/0/1/2`.
    - `main.py`: signal counts printout.
  - Acceptance: backtest on BTC/USDT from 2025-01-01 produces > 0 trades; a unit test with synthetic data confirms an entry bar is not masked by exit conditions.

- [ ] **2. `backtest.py:_close` — short-trade accounting injects phantom equity**
  - Problem: opening a short only deducts commission, but closing adds `pos["notional"] + pnl` — the notional was never deducted, so every short adds ~full notional to equity. Entry commission is also effectively double-counted (it's inside `pnl` already).
  - Fix: make the net equity effect of a round-trip short exactly `pnl` (which is net of both commissions). Open: `equity -= entry_comm`. Close: `equity += pnl + entry_comm`.
  - Acceptance: unit test — short opened and closed at the same price loses exactly entry_comm + exit_comm; equity never jumps by notional.

- [ ] **3. `live_trader.py:_decide_action` — news-only entries contradict the documented policy**
  - Problem: lines ~559–560 and ~570–571 open positions on weak sentiment alone (`weak_bull and not tech_long → open_long`, same for shorts). Docstring and `config.py` both say weak sentiment requires technical confirmation. With bug #1, sentiment is currently the only entry driver.
  - Fix: change `weak_bull and not tech_long` → `"skip"`, and `weak_bear and not tech_short` → `"skip"`. Keep strong-sentiment-only entries (`strong_bull` / `strong_bear`) as-is. Update unit tests for the decision matrix.

- [ ] **4. `trading_env.py` — file cannot run (typos/syntax)**
  - Fix all of:
    - `np.title(...)` → `np.tile(...)` (line ~48)
    - `self.df.lot[self.current_step, "Close"]` → `self.df.loc[...]` (line ~57)
    - `pip_value - 0.0001` → `pip_value = 0.0001` (line ~65)
    - `sl_price_distance = pip_value` → `sl_price_distance = sl * pip_value` (line ~66)
    - `tp_price_distance = tp = pip_value` → `tp_price_distance = tp * pip_value` (line ~67)
    - `exit_proce` → `exit_price` (line ~122)
    - `self.last_trae_info` → `self.last_trade_info` (line ~39)
    - `def render(seld, ...)` → `def render(self, ...)` (line ~150)
    - `_calculate_reward(self, direction, s1, tp)` → param name `sl` (and use it)
    - class name `ForextTradingEnv` → `ForexTradingEnv`
  - Also: migrate `gym` → `gymnasium` (new API: `reset()` returns `(obs, info)`, `step()` returns 5-tuple); `self.equity += reward` mixes pip-points with currency — accumulate `pnl`, not `reward`; duplicate branch in the long ambiguous SL/TP case (first two branches both return `-sl_price_distance`); `equity_curve` is never appended.
  - Note: this file is forex-oriented (pip = 0.0001) and disconnected from the rest of the crypto bot. If RL isn't planned, consider deleting `trading_env.py` + `train_agent.py` instead of fixing. **Ask the owner before deleting.**

- [ ] **5. `dashboard.py` — crashes on macOS**
  - Problem: `os.startfile` is Windows-only.
  - Fix: use `webbrowser.open(f"file://{tmp.name}")`.
  - Also: the hardcoded HTML documents stale parameters (RSI 38, EMA 20/50, $10,000 capital, 2% risk vs. actual config 46, 9/21, $28, 20%). Either interpolate values from `config.py` into the HTML, or remove the hardcoded numbers.

---

## P0.5 — Tests (do immediately after P0, lock fixes in)

- [ ] **6. Add a `tests/` directory with pytest**
  - `test_strategy.py`: synthetic OHLCV where one bar must produce a long entry, one a short entry, one each exit — assert booleans fire and don't mask each other.
  - `test_backtest.py`: hand-computed long and short round trips (including commissions + slippage) — assert final equity matches to the cent; assert stop-loss exit uses the stop price.
  - `test_decision_engine.py`: table-driven test of `_decide_action` covering the full documented matrix.
  - `test_indicators.py`: assert no look-ahead — the trend bias visible on a 15m bar must come from the *previous closed* higher-TF candle (see #7).
  - Add `pytest` to requirements; tests must run offline (no network).

---

## P1 — Backtest validity (results are currently misleading)

- [ ] **7. `indicators.py:build` — look-ahead bias in higher-timeframe merge**
  - Problem: 1h/4h candles are timestamped at their open, then joined + ffilled onto 15m bars, so 15m bars inside a candle see that candle's close before it exists.
  - Fix: shift the trend columns by one bar on their own timeframe before joining: `tf1[trend_cols].shift(1)`, same for tf2. This also matches live behavior (live only uses closed bars).

- [ ] **8. `backtest.py` — stop-loss only checked on close**
  - Fix: check stop against bar `low` (long) / `high` (short); fill at the stop price. If both stop and exit-signal occur on the same bar, assume stop first (pessimistic).

- [ ] **9. `backtest.py:_metrics` — wrong Sharpe and drawdown**
  - Sharpe: build a daily equity series (resample trade PnL to daily, on the equity curve) and annualize daily returns with √365 (crypto trades 7 days/week). Do not annualize per-trade returns with √252.
  - Max drawdown: compute from the running equity curve (including open-position mark-to-market if feasible; at minimum from equity after each trade), relative to the running peak — not `cumsum / initial_capital`.

- [ ] **10. `backtest.py` — missing short borrow cost**
  - Fix: add `MARGIN_INTEREST_HOURLY` to `config.py` (Binance cross-margin BTC ≈ 0.0009%/h — make it configurable, verify current rate) and deduct `notional × rate × hours_held` from short PnL.

- [ ] **11. Backtest/live parity — news layer is never backtested**
  - The live `_decide_action` (news + technical) has no backtest equivalent. Minimum: add a note/flag in README that backtest covers technical-only. Better: make `backtest.run` accept an optional news-score series and route decisions through the same `_decide_action` function (move it to a shared module, e.g. `decision.py`, imported by both).

---

## P2 — Performance

- [ ] **12. `backtest.py:run` — replace `iterrows()`**
  - Convert the needed columns to numpy arrays once and loop over indices (or use `itertuples`). The optimizer runs this 100+ times; target ≥10x speedup. Keep results bit-identical to the post-P0/P1 version (assert in a test).

- [ ] **13. `data_fetcher.py` — no caching, redundant fetches**
  - Cache raw OHLCV to local parquet (e.g. `data/{symbol}_{tf}.parquet`), fetch only missing bars after the last cached timestamp on subsequent runs.
  - Derive 1h and 4h frames by resampling the 15m data (`resample("1h"/"4h")` with OHLCV agg) instead of three separate paginated downloads — fewer API calls and guaranteed alignment. Keep `fetch_ohlcv` for the base TF only.

- [ ] **14. `optimizer.py` — pruner is a no-op, and in-sample overfitting**
  - `MedianPruner` does nothing because `trial.report()` is never called — either remove the pruner or report intermediate scores.
  - Add walk-forward validation: split the lookback into train/test (e.g. 70/30 by time); optimize on train, report and select on test score. Refuse to save params whose test performance is drastically worse than train.
  - Wrap `study.best_params` access — if every trial was pruned, exit with a clear message instead of an exception.

---

## P3 — Live-trading robustness

- [ ] **15. `live_trader.py` — stops must live on the exchange, not in the bot**
  - After every entry fill, place an exchange-side stop order (stop-loss-limit, or OCO if combining with take-profit) for the position. Cancel/replace it when the position is closed by signal. Keep the existing bot-side check as a backup only.
  - Acceptance: kill the bot with an open position → stop order still visible on Binance.

- [ ] **16. `live_trader.py` — orphaned positions and missing order reconciliation**
  - If an order fills but the process dies before `_save_state`, the position is untracked. Fixes:
    - Save a "pending entry" state record *before* sending the order; reconcile on the next cycle via `fetch_order`.
    - Extend `_sync_positions_from_exchange` to *adopt* exchange positions not present in local state (currently it only removes stale local entries), reconstructing entry price from recent fills (`fetch_my_trades`) and applying a fresh stop.

- [ ] **17. `live_trader.py` — PnL ignores fees and interest**
  - Deduct taker fees (both sides) and margin interest in `_close_long` / `_close_short` PnL, so `trades_log.csv` and the daily circuit breaker see real PnL. Pull actual fee from the order response (`order["fee"]` / `order["fees"]`) when available, else `config.COMMISSION`.

- [ ] **18. `news_analyzer.py` — RSS fetch can hang the trading loop**
  - `feedparser.parse(url)` has no timeout. Fetch with `requests.get(url, timeout=10)` and pass `response.content` to `feedparser.parse`, or set `socket.setdefaulttimeout`. A dead feed must never block a trading cycle.

- [ ] **19. `live_trader.py` — misc safety**
  - `_calc_qty`: if free balance < `_MIN_NOTIONAL`, skip the trade (currently still orders 5.5 USDT).
  - Log rotation: switch `FileHandler` → `RotatingFileHandler` (e.g. 5 MB × 3).
  - Circuit breaker: also count unrealized drawdown of open positions, not just realized PnL.

---

## P4 — Consistency & hygiene

- [ ] **20. Unify position sizing**
  - Three schemes disagree: `config.RISK_PER_TRADE = 0.20`, optimizer tunes 0.01–0.05, live floors at 5.5 USDT min-notional. Decide one model (suggest: `RISK_PER_TRADE` of equity with a min-notional guard in *both* backtest and live; remove `RISK_PER_TRADE` from the optimizer search space for an account this small) and apply everywhere.

- [ ] **21. `config.py` / `live_trader.py` — dead and stale config**
  - `NEWS_WEAK_BEAR` and `NEWS_STRONG_BEAR` are defined but never used (`_decide_action` negates the bull thresholds instead). Use them, or delete them.
  - Comment says "1.5% stop-loss" but `STOP_LOSS_PCT = 0.012` — fix the comment.
  - `MARGIN_TYPE` only affects log text; order params hardcode `"cross"`. Wire it through or remove the setting.

- [ ] **22. `requirements.txt`**
  - Pin versions. Remove unused `yfinance`. Add missing: `pytest`, and (only if keeping the RL files) `gymnasium`, `stable-baselines3`, `matplotlib`.

- [ ] **23. `web_ui.py` — unsafe controls**
  - Start button runs `live_trader.py --live` (real money) with no confirmation. Default to testnet; require an explicit `mode=live` parameter + a typed confirmation in the UI.
  - `pgrep -f live_trader.py` matches any process whose command line contains the string (e.g. an open editor) — match the exact python invocation or use a pidfile written by `live_trader.py`.
  - Document (or enforce) that the UI must not be exposed on `0.0.0.0` over plain HTTP with Basic auth — bind `127.0.0.1` by default and add a `--host` flag.

- [ ] **24. Decide fate of `train_agent.py`**
  - Currently 5 lines of imports (`import matplotlib as plt` is also wrong — should be `matplotlib.pyplot`). Either implement training (PPO on the fixed env) or delete alongside `trading_env.py`. **Ask the owner.**

---

## Verification (after all fixes)

- [ ] `pytest` passes.
- [ ] `python main.py` runs end-to-end and reports a non-zero trade count.
- [ ] `python optimizer.py --trials 20` completes without exceptions and best params beat baseline on the *test* split.
- [ ] `python live_trader.py --once` runs clean on testnet keys.
- [ ] `python dashboard.py` opens on macOS.
- [ ] Re-run the backtest before/after #7 and #8 and note the performance delta in the PR description (expect results to get *worse* — that's correct; the old numbers were inflated).
