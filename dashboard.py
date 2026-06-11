"""
dashboard.py
Generates an interactive HTML flow-chart of the trading bot's architecture
and opens it in the default browser.

Usage
-----
    python dashboard.py
"""

import os
import tempfile
import webbrowser

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>Trading Bot — Architecture Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    min-height: 100vh;
    padding: 24px;
  }

  h1 {
    text-align: center;
    font-size: 1.6rem;
    letter-spacing: .05em;
    color: #58a6ff;
    margin-bottom: 6px;
  }
  .subtitle {
    text-align: center;
    font-size: .85rem;
    color: #8b949e;
    margin-bottom: 32px;
  }

  /* ── legend ── */
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: center;
    margin-bottom: 36px;
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: .78rem;
    color: #8b949e;
  }
  .legend-dot {
    width: 12px; height: 12px;
    border-radius: 3px;
  }

  /* ── pipeline row ── */
  .pipeline {
    display: flex;
    align-items: flex-start;
    gap: 0;
    justify-content: center;
    margin-bottom: 40px;
    flex-wrap: nowrap;
    overflow-x: auto;
    padding-bottom: 8px;
  }

  .stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 160px;
  }

  .stage-box {
    width: 150px;
    border-radius: 10px;
    border: 2px solid;
    padding: 12px 10px;
    text-align: center;
    cursor: pointer;
    transition: transform .15s, box-shadow .15s;
    position: relative;
  }
  .stage-box:hover { transform: translateY(-3px); box-shadow: 0 8px 24px rgba(0,0,0,.5); }

  .stage-box .module { font-size: .65rem; letter-spacing: .08em; text-transform: uppercase; opacity: .7; margin-bottom: 4px; }
  .stage-box .fn     { font-size: .82rem; font-weight: 600; }
  .stage-box .desc   { font-size: .68rem; margin-top: 6px; opacity: .75; line-height: 1.35; }

  .arrow {
    display: flex;
    align-items: center;
    padding-top: 28px;   /* align with box vertical centre */
    color: #30363d;
    font-size: 1.4rem;
    flex-shrink: 0;
    user-select: none;
  }
  .arrow span { color: #3d4f6a; font-size: 1.8rem; line-height: 1; }

  /* data-label under arrow */
  .arrow-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    flex-shrink: 0;
  }
  .data-label {
    font-size: .62rem;
    color: #484f58;
    margin-top: 32px;
    text-align: center;
    max-width: 80px;
  }

  /* ── module detail cards ── */
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 20px;
    max-width: 1300px;
    margin: 0 auto;
  }

  .card {
    background: #161b22;
    border-radius: 10px;
    border: 1px solid #30363d;
    overflow: hidden;
  }
  .card-header {
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .card-header .dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .card-header h3 { font-size: .95rem; }
  .card-header .file { font-size: .72rem; color: #8b949e; margin-left: auto; font-family: monospace; }

  .fn-list { list-style: none; }
  .fn-list li {
    padding: 9px 16px;
    border-top: 1px solid #21262d;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .fn-list li:hover { background: #1c2128; }
  .fn-sig {
    font-family: 'Cascadia Code', 'Fira Code', monospace;
    font-size: .77rem;
    color: #d2a8ff;
  }
  .fn-desc { font-size: .73rem; color: #8b949e; line-height: 1.4; }
  .fn-returns { font-size: .7rem; color: #56d364; margin-top: 2px; }
  .fn-calls {
    font-size: .68rem;
    color: #58a6ff;
    margin-top: 3px;
  }

  /* ── data flow section ── */
  .flow-section {
    max-width: 1300px;
    margin: 40px auto 0;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 20px 24px;
  }
  .flow-section h2 { font-size: 1rem; color: #58a6ff; margin-bottom: 16px; }

  .flow-tree { font-family: monospace; font-size: .82rem; line-height: 1.9; color: #c9d1d9; }
  .flow-tree .call   { color: #d2a8ff; }
  .flow-tree .data   { color: #56d364; font-style: italic; }
  .flow-tree .note   { color: #8b949e; }
  .flow-tree .module { color: #58a6ff; }

  /* ── colour palette (CSS vars) ── */
  :root {
    --c-config:   #e6a817;
    --c-fetcher:  #58a6ff;
    --c-indic:    #d2a8ff;
    --c-strat:    #56d364;
    --c-back:     #f0883e;
    --c-main:     #ff7b72;
  }

  /* ── signal key ── */
  .signal-key {
    max-width: 1300px;
    margin: 20px auto 0;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 20px 24px;
  }
  .signal-key h2 { font-size: 1rem; color: #58a6ff; margin-bottom: 14px; }
  .signal-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .signal-card {
    padding: 10px 14px;
    border-radius: 7px;
    border: 1px solid #30363d;
  }
  .signal-card .sig-val { font-family: monospace; font-size: 1.3rem; font-weight: 700; }
  .signal-card .sig-name { font-size: .8rem; margin: 2px 0 4px; }
  .signal-card .sig-cond { font-size: .72rem; color: #8b949e; line-height: 1.5; }
</style>
</head>
<body>

<h1>Trading Bot — Architecture Dashboard</h1>
<p class="subtitle">Multi-Timeframe BB Mean Reversion + RSI Confluence &nbsp;|&nbsp; 15m entry · 1h trend · 4h macro</p>

<!-- ── Legend ── -->
<div class="legend">
  <div class="legend-item"><div class="legend-dot" style="background:var(--c-main)"></div>main.py</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--c-config)"></div>config.py</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--c-fetcher)"></div>data_fetcher.py</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--c-indic)"></div>indicators.py</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--c-strat)"></div>strategy.py</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--c-back)"></div>backtest.py</div>
</div>

<!-- ── Pipeline ── -->
<div class="pipeline">

  <!-- main -->
  <div class="stage">
    <div class="stage-box" style="border-color:var(--c-main); background:#1c1012;">
      <div class="module">main.py</div>
      <div class="fn">main()</div>
      <div class="desc">Entry point.<br>Loops over each symbol, prints summary table.</div>
    </div>
  </div>

  <div class="arrow-wrap">
    <div class="arrow"><span>&#8594;</span></div>
    <div class="data-label">symbol<br>string</div>
  </div>

  <!-- data fetcher -->
  <div class="stage">
    <div class="stage-box" style="border-color:var(--c-fetcher); background:#0d1520;">
      <div class="module">data_fetcher.py</div>
      <div class="fn">fetch_all_timeframes()</div>
      <div class="desc">Downloads OHLCV bars for 15m, 1h, 4h from Binance via ccxt.</div>
    </div>
  </div>

  <div class="arrow-wrap">
    <div class="arrow"><span>&#8594;</span></div>
    <div class="data-label">dict[tf → DataFrame]<br>raw OHLCV</div>
  </div>

  <!-- indicators -->
  <div class="stage">
    <div class="stage-box" style="border-color:var(--c-indic); background:#160d20;">
      <div class="module">indicators.py</div>
      <div class="fn">build(frames)</div>
      <div class="desc">Computes BB, RSI on 15m; EMA bias on 1h & 4h; merges into one DataFrame.</div>
    </div>
  </div>

  <div class="arrow-wrap">
    <div class="arrow"><span>&#8594;</span></div>
    <div class="data-label">DataFrame<br>+ indicators</div>
  </div>

  <!-- strategy -->
  <div class="stage">
    <div class="stage-box" style="border-color:var(--c-strat); background:#0d1a0d;">
      <div class="module">strategy.py</div>
      <div class="fn">generate_signals()</div>
      <div class="desc">Adds boolean columns: long_entry, long_exit, short_entry, short_exit.</div>
    </div>
  </div>

  <div class="arrow-wrap">
    <div class="arrow"><span>&#8594;</span></div>
    <div class="data-label">DataFrame<br>+ signal bools</div>
  </div>

  <!-- backtest -->
  <div class="stage">
    <div class="stage-box" style="border-color:var(--c-back); background:#1c1208;">
      <div class="module">backtest.py</div>
      <div class="fn">run(df)</div>
      <div class="desc">Event-driven loop: open/close positions, apply stop-loss, compute metrics.</div>
    </div>
  </div>

  <div class="arrow-wrap">
    <div class="arrow"><span>&#8594;</span></div>
    <div class="data-label">metrics dict<br>+ trades DF</div>
  </div>

  <!-- output -->
  <div class="stage">
    <div class="stage-box" style="border-color:#30363d; background:#161b22;">
      <div class="module">output</div>
      <div class="fn">Console + Tables</div>
      <div class="desc">tabulate prints per-symbol metrics and cross-symbol summary.</div>
    </div>
  </div>

</div><!-- end pipeline -->


<!-- ── Module Detail Cards ── -->
<div class="cards-grid">

  <!-- config.py -->
  <div class="card">
    <div class="card-header" style="background:#1c1a0d;">
      <div class="dot" style="background:var(--c-config)"></div>
      <h3>config.py</h3>
      <span class="file">config.py</span>
    </div>
    <ul class="fn-list">
      <li>
        <span class="fn-sig">CRYPTO_SYMBOLS</span>
        <span class="fn-desc">List of trading pairs to backtest. Current: {CONFIG_SYMBOLS}</span>
      </li>
      <li>
        <span class="fn-sig">BASE_TF / TREND_TF1 / TREND_TF2</span>
        <span class="fn-desc">Timeframe ladder: 15m (entry), 1h (intermediate trend), 4h (macro trend)</span>
      </li>
      <li>
        <span class="fn-sig">BB_PERIOD, BB_STD</span>
        <span class="fn-desc">Bollinger Band window ({CONFIG_BB_PERIOD}) and standard deviation multiplier ({CONFIG_BB_STD})</span>
      </li>
      <li>
        <span class="fn-sig">RSI_PERIOD, RSI_LONG/SHORT_ENTRY/EXIT</span>
        <span class="fn-desc">RSI({CONFIG_RSI_PERIOD}) thresholds: entry long &lt;{CONFIG_RSI_LONG_ENTRY}, entry short &gt;{CONFIG_RSI_SHORT_ENTRY}, exit long &gt;{CONFIG_RSI_LONG_EXIT}, exit short &lt;{CONFIG_RSI_SHORT_EXIT}</span>
      </li>
      <li>
        <span class="fn-sig">EMA_TREND1 / EMA_TREND2</span>
        <span class="fn-desc">EMA periods: {CONFIG_EMA_TREND1} on 1h bars, {CONFIG_EMA_TREND2} on 4h bars</span>
      </li>
      <li>
        <span class="fn-sig">INITIAL_CAPITAL, RISK_PER_TRADE, STOP_LOSS_PCT, COMMISSION</span>
        <span class="fn-desc">{CONFIG_INITIAL_CAPITAL} starting capital · {CONFIG_RISK_PER_TRADE} risk/trade · {CONFIG_STOP_LOSS_PCT} stop-loss · {CONFIG_COMMISSION} commission/side</span>
      </li>
    </ul>
  </div>

  <!-- data_fetcher.py -->
  <div class="card">
    <div class="card-header" style="background:#0d1520;">
      <div class="dot" style="background:var(--c-fetcher)"></div>
      <h3>data_fetcher.py</h3>
      <span class="file">data_fetcher.py</span>
    </div>
    <ul class="fn-list">
      <li>
        <span class="fn-sig">_since_ms(date_str)</span>
        <span class="fn-desc">Converts "YYYY-MM-DD" string to Unix millisecond timestamp in UTC.</span>
        <span class="fn-returns">→ int (ms timestamp)</span>
      </li>
      <li>
        <span class="fn-sig">fetch_ohlcv(symbol, timeframe, start_date, exchange_id)</span>
        <span class="fn-desc">Paginates ccxt exchange in 1 000-bar chunks from start_date to now. Deduplicates rows, returns clean DataFrame with DatetimeIndex (UTC).</span>
        <span class="fn-returns">→ DataFrame [open, high, low, close, volume]</span>
        <span class="fn-calls">calls: _since_ms · ccxt.{exchange}.fetch_ohlcv</span>
      </li>
      <li>
        <span class="fn-sig">fetch_all_timeframes(symbol)</span>
        <span class="fn-desc">Calls fetch_ohlcv once per timeframe (BASE_TF, TREND_TF1, TREND_TF2). Returns a dict keyed by timeframe string.</span>
        <span class="fn-returns">→ dict[str, DataFrame]</span>
        <span class="fn-calls">calls: fetch_ohlcv ×3</span>
      </li>
    </ul>
  </div>

  <!-- indicators.py -->
  <div class="card">
    <div class="card-header" style="background:#160d20;">
      <div class="dot" style="background:var(--c-indic)"></div>
      <h3>indicators.py</h3>
      <span class="file">indicators.py</span>
    </div>
    <ul class="fn-list">
      <li>
        <span class="fn-sig">_ema(series, n)</span>
        <span class="fn-desc">Exponential moving average with span=n (adjust=False).</span>
        <span class="fn-returns">→ pd.Series</span>
      </li>
      <li>
        <span class="fn-sig">_rsi(series, n)</span>
        <span class="fn-desc">Wilder RSI using exponential smoothing (com = n-1). Handles zero-loss division safely.</span>
        <span class="fn-returns">→ pd.Series (0–100)</span>
      </li>
      <li>
        <span class="fn-sig">_bollinger(series, n, k)</span>
        <span class="fn-desc">Rolling mean ± k×std. Uses ddof=0 for population std.</span>
        <span class="fn-returns">→ (upper, mid, lower) Series tuple</span>
      </li>
      <li>
        <span class="fn-sig">_add_base_indicators(df)</span>
        <span class="fn-desc">Adds bb_upper, bb_mid, bb_lower, rsi columns to 15m DataFrame.</span>
        <span class="fn-calls">calls: _bollinger · _rsi</span>
      </li>
      <li>
        <span class="fn-sig">_add_trend_indicators(df, ema_period, prefix)</span>
        <span class="fn-desc">Adds {prefix}_ema and {prefix}_bias (1=bullish, -1=bearish) to a higher-TF DataFrame.</span>
        <span class="fn-calls">calls: _ema</span>
      </li>
      <li>
        <span class="fn-sig">build(frames)</span>
        <span class="fn-desc">Orchestrates indicator computation, left-joins trend columns onto base DataFrame using forward-fill, drops NaN warm-up rows.</span>
        <span class="fn-returns">→ DataFrame (15m resolution, all columns merged)</span>
        <span class="fn-calls">calls: _add_base_indicators · _add_trend_indicators ×2</span>
      </li>
    </ul>
  </div>

  <!-- strategy.py -->
  <div class="card">
    <div class="card-header" style="background:#0d1a0d;">
      <div class="dot" style="background:var(--c-strat)"></div>
      <h3>strategy.py</h3>
      <span class="file">strategy.py</span>
    </div>
    <ul class="fn-list">
      <li>
        <span class="fn-sig">generate_signals(df)</span>
        <span class="fn-desc">Evaluates entry/exit conditions on each bar and sets boolean columns: long_entry, long_exit, short_entry, short_exit.
        </span>
        <span class="fn-returns">→ DataFrame (with boolean signal columns added)</span>
      </li>
    </ul>
  </div>

  <!-- backtest.py -->
  <div class="card">
    <div class="card-header" style="background:#1c1208;">
      <div class="dot" style="background:var(--c-back)"></div>
      <h3>backtest.py</h3>
      <span class="file">backtest.py</span>
    </div>
    <ul class="fn-list">
      <li>
        <span class="fn-sig">run(df)</span>
        <span class="fn-desc">Main backtest loop. Iterates bars, checks stop-loss / exit signals on open positions, opens new positions on entry signals, force-closes at last bar.</span>
        <span class="fn-returns">→ (metrics dict, trades DataFrame)</span>
        <span class="fn-calls">calls: _open · _close · _record · _metrics</span>
      </li>
      <li>
        <span class="fn-sig">_open(side, price, ts, equity)</span>
        <span class="fn-desc">Sizes position (equity × RISK_PER_TRADE), deducts commission, sets stop-loss price. Returns position dict and updated equity.</span>
        <span class="fn-returns">→ (position dict, float equity)</span>
      </li>
      <li>
        <span class="fn-sig">_close(pos, exit_price, equity)</span>
        <span class="fn-desc">Computes PnL from proceeds minus commissions for long or short, returns PnL and updated equity.</span>
        <span class="fn-returns">→ (float pnl, float equity)</span>
      </li>
      <li>
        <span class="fn-sig">_record(pos, exit_px, exit_ts, pnl, note)</span>
        <span class="fn-desc">Builds a trade log dict with side, entry/exit time & price, qty, pnl_usd, pnl_pct. Appends "note" only if non-empty (e.g. "stop-loss", "forced close").</span>
        <span class="fn-returns">→ dict (one row for trades DataFrame)</span>
      </li>
      <li>
        <span class="fn-sig">_metrics(trades, final_equity)</span>
        <span class="fn-desc">Computes: return%, num_trades, win_rate, avg_win/loss, profit_factor, max_drawdown, Sharpe ratio, long/short count, stop-loss hits.</span>
        <span class="fn-returns">→ dict of performance metrics</span>
      </li>
    </ul>
  </div>

  <!-- main.py -->
  <div class="card">
    <div class="card-header" style="background:#1c1012;">
      <div class="dot" style="background:var(--c-main)"></div>
      <h3>main.py</h3>
      <span class="file">main.py</span>
    </div>
    <ul class="fn-list">
      <li>
        <span class="fn-sig">run_symbol(symbol)</span>
        <span class="fn-desc">Full pipeline for one symbol: fetch → build indicators → generate signals → backtest → print metrics table and last-10-trades table.</span>
        <span class="fn-returns">→ dict {symbol, ...metrics}</span>
        <span class="fn-calls">calls: data_fetcher.fetch_all_timeframes · indicators.build · strategy.generate_signals · backtest.run</span>
      </li>
      <li>
        <span class="fn-sig">main()</span>
        <span class="fn-desc">Prints strategy header, loops over config.CRYPTO_SYMBOLS calling run_symbol(), collects results, prints cross-symbol summary table via tabulate.</span>
        <span class="fn-calls">calls: run_symbol · tabulate</span>
      </li>
    </ul>
  </div>

</div><!-- end cards grid -->


<!-- ── Call Tree ── -->
<div class="flow-section">
  <h2>Full Call Tree</h2>
  <div class="flow-tree">
<span class="module">main</span>.<span class="call">main()</span>
  └─ <span class="module">main</span>.<span class="call">run_symbol(symbol)</span>   <span class="note"># once per CRYPTO_SYMBOL</span>
       │
       ├─ <span class="module">data_fetcher</span>.<span class="call">fetch_all_timeframes(symbol)</span>
       │    ├─ <span class="call">fetch_ohlcv(symbol, "15m")</span>   <span class="note"># BASE_TF</span>
       │    │    └─ <span class="call">_since_ms(START_DATE)</span>  →  <span class="data">int (ms)</span>
       │    │    └─ ccxt loop  →  <span class="data">DataFrame [open high low close volume]</span>
       │    ├─ <span class="call">fetch_ohlcv(symbol, "1h")</span>    <span class="note"># TREND_TF1</span>
       │    └─ <span class="call">fetch_ohlcv(symbol, "4h")</span>    <span class="note"># TREND_TF2</span>
       │    →  <span class="data">dict[tf → DataFrame]</span>
       │
       ├─ <span class="module">indicators</span>.<span class="call">build(frames)</span>
       │    ├─ <span class="call">_add_base_indicators(frames["15m"])</span>
       │    │    ├─ <span class="call">_bollinger(close, 20, 2.0)</span>  →  <span class="data">bb_upper, bb_mid, bb_lower</span>
       │    │    └─ <span class="call">_rsi(close, 14)</span>             →  <span class="data">rsi</span>
       │    ├─ <span class="call">_add_trend_indicators(frames["1h"], 20, "trend1")</span>
       │    │    └─ <span class="call">_ema(close, 20)</span>  →  <span class="data">trend1_ema, trend1_bias</span>
       │    ├─ <span class="call">_add_trend_indicators(frames["4h"], 50, "trend2")</span>
       │    │    └─ <span class="call">_ema(close, 50)</span>  →  <span class="data">trend2_ema, trend2_bias</span>
       │    └─ left-join + ffill + dropna
       │    →  <span class="data">DataFrame (15m, all columns)</span>
       │
       ├─ <span class="module">strategy</span>.<span class="call">generate_signals(df)</span>
       │    →  <span class="data">DataFrame + boolean signal columns</span>
       │
       └─ <span class="module">backtest</span>.<span class="call">run(df)</span>
            ├─ per-bar loop:
            │    ├─ <span class="call">_close(pos, price, equity)</span>   <span class="note"># on stop-loss or exit signal</span>
            │    ├─ <span class="call">_record(pos, exit_px, ...)</span>   <span class="note"># append to trade log</span>
            │    └─ <span class="call">_open(side, price, ts, equity)</span>  <span class="note"># on entry signal</span>
            └─ <span class="call">_metrics(trades_df, equity)</span>
            →  <span class="data">(metrics dict, trades DataFrame)</span>
  </div>
</div>

<!-- ── Signal Key ── -->
<div class="signal-key">
  <h2>Signal Convention</h2>
  <div class="signal-grid">
    <div class="signal-card" style="background:#0d1a0d; border-color:#56d364;">
      <div class="sig-val" style="color:#56d364;">long_entry</div>
      <div class="sig-name">Open Long</div>
      <div class="sig-cond">
        close ≤ bb_lower<br>
        RSI &lt; {CONFIG_RSI_LONG_ENTRY}<br>
        trend1_bias = 1  (1h bullish)<br>
        trend2_bias = 1  (4h bullish)
      </div>
    </div>
    <div class="signal-card" style="background:#1a0d0d; border-color:#ff7b72;">
      <div class="sig-val" style="color:#ff7b72;">long_exit</div>
      <div class="sig-name">Close Long</div>
      <div class="sig-cond">
        close ≥ bb_mid  OR<br>
        RSI &gt; {CONFIG_RSI_LONG_EXIT}  OR<br>
        trend1_bias turns −1
      </div>
    </div>
    <div class="signal-card" style="background:#1a0d0d; border-color:#f0883e;">
      <div class="sig-val" style="color:#f0883e;">short_entry</div>
      <div class="sig-name">Open Short</div>
      <div class="sig-cond">
        close ≥ bb_upper<br>
        RSI &gt; {CONFIG_RSI_SHORT_ENTRY}<br>
        trend1_bias = −1  (1h bearish)<br>
        trend2_bias = −1  (4h bearish)
      </div>
    </div>
    <div class="signal-card" style="background:#0d1a0d; border-color:#56d364;">
      <div class="sig-val" style="color:#56d364;">short_exit</div>
      <div class="sig-name">Close Short</div>
      <div class="sig-cond">
        close ≤ bb_mid  OR<br>
        RSI &lt; {CONFIG_RSI_SHORT_EXIT}  OR<br>
        trend1_bias turns +1
      </div>
    </div>
  </div>
</div>

<p style="text-align:center; color:#484f58; font-size:.72rem; margin-top:32px; margin-bottom:8px;">
  Generated by dashboard.py &nbsp;·&nbsp; Trading Bot V.1
</p>

</body>
</html>
"""


def main() -> None:
    import config

    html = HTML
    # Interpolate actual config values into the HTML
    replacements = {
        "{CONFIG_SYMBOLS}": str(config.CRYPTO_SYMBOLS),
        "{CONFIG_BB_PERIOD}": str(config.BB_PERIOD),
        "{CONFIG_BB_STD}": str(config.BB_STD),
        "{CONFIG_RSI_PERIOD}": str(config.RSI_PERIOD),
        "{CONFIG_RSI_LONG_ENTRY}": str(config.RSI_LONG_ENTRY),
        "{CONFIG_RSI_SHORT_ENTRY}": str(config.RSI_SHORT_ENTRY),
        "{CONFIG_RSI_LONG_EXIT}": str(config.RSI_LONG_EXIT),
        "{CONFIG_RSI_SHORT_EXIT}": str(config.RSI_SHORT_EXIT),
        "{CONFIG_EMA_TREND1}": str(config.EMA_TREND1),
        "{CONFIG_EMA_TREND2}": str(config.EMA_TREND2),
        "{CONFIG_INITIAL_CAPITAL}": f"${config.INITIAL_CAPITAL:,}",
        "{CONFIG_RISK_PER_TRADE}": f"{config.RISK_PER_TRADE*100:.0f}%",
        "{CONFIG_STOP_LOSS_PCT}": f"{config.STOP_LOSS_PCT*100:.1f}%",
        "{CONFIG_COMMISSION}": f"{config.COMMISSION*100:.1f}%",
    }
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    # Write to a temp file and open in browser
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8"
    )
    tmp.write(html)
    tmp.close()

    print(f"Opening dashboard: {tmp.name}")
    webbrowser.open(f"file://{tmp.name}")
    print("Dashboard opened in your default browser.")
    print(f"(Temp file: {tmp.name})")


if __name__ == "__main__":
    main()
