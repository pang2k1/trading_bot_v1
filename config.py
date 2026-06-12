# ── Symbols ──────────────────────────────────────────────────────────────────
# Small account: one symbol only — avoids spreading a tiny balance too thin
CRYPTO_SYMBOLS = ["BTC/USDT"]

# ── Multi-Timeframe Setup ─────────────────────────────────────────────────────
# Entry signals fire on the BASE timeframe.
# Higher timeframes provide trend bias filters.
BASE_TF   = "15m"   # where entries are triggered
TREND_TF1 = "1h"    # intermediate trend filter
TREND_TF2 = "4h"    # macro trend filter

# ── Backtest date range ───────────────────────────────────────────────────────
START_DATE = "2025-01-01"   # ISO string UTC

# ── Indicators ────────────────────────────────────────────────────────────────
# Bollinger Bands  (on BASE_TF)
BB_PERIOD = 20
BB_STD    = 2.0

# RSI  (on BASE_TF)
RSI_PERIOD      = 14
RSI_LONG_ENTRY  = 46    # enter long when RSI is this low (oversold dip)
RSI_SHORT_ENTRY = 60    # enter short when RSI is this high (overbought)
RSI_LONG_EXIT   = 63    # exit long when RSI climbs here
RSI_SHORT_EXIT  = 42    # exit short when RSI drops here

# Trend EMAs  (computed on each TF's own candles)
EMA_TREND1 = 9     # applied to TREND_TF1 (1h EMA-9)
EMA_TREND2 = 21    # applied to TREND_TF2 (4h EMA-21)

# ── Risk / position sizing ────────────────────────────────────────────────────
# Account size: ~1000 THB ≈ 28 USDT (at ~35 THB/USD)
# INITIAL_CAPITAL is used for backtesting only — live trading uses real balance.
# RISK_PER_TRADE = fraction of balance used as trade notional (position size).
#   Binance minimum order is ~5 USDT, so this must stay high enough for small accounts.
#   At 20% of $28 = ~$5.60 notional — above the $5 minimum.
#   Stop-loss at 1.2% means max loss per trade ≈ $0.07 + fees (0.3% of account).
INITIAL_CAPITAL = 28          # USDT equivalent of 1000 THB
RISK_PER_TRADE  = 0.20        # 20% of balance per trade (small account — must meet Binance $5 minimum)
STOP_LOSS_PCT   = 0.012       # 1.2% stop-loss (widened for real-time intra-candle checks)
COMMISSION      = 0.001       # 0.1% per side (Binance taker — hold BNB to reduce to 0.075%)

# ── Slippage ──────────────────────────────────────────────────────────────────
# Applied in backtesting to simulate real fill prices on market orders.
# 0.05% is conservative for BTC/USDT on Binance (usually much less).
SLIPPAGE_PCT = 0.0005         # 0.05% slippage per fill

# ── Circuit breaker ───────────────────────────────────────────────────────────
# Stop trading for the rest of the day if total losses exceed this fraction of
# the starting daily balance. Resets at UTC midnight.
MAX_DAILY_LOSS_PCT = 0.03     # 3% max daily drawdown before halting

# ── Long-only mode ────────────────────────────────────────────────────────────
# True  = only trade longs (Binance Spot).
# False = trade both longs and shorts (requires Binance Margin or Futures).
LONG_ONLY = False

# ── Margin trading settings ───────────────────────────────────────────────────
# MARGIN_TYPE: "cross"    = whole account balance acts as collateral for all pairs.
#              "isolated" = each pair has its own collateral pool (lower liquidation risk).
# Note: leverage on margin is implicitly determined by how much you borrow relative
# to your own balance. Binance cross-margin supports up to 3x; isolated up to 10x
# depending on the asset. There is no programmatic leverage setter — borrow ratio
# is controlled by how large a position you open relative to your free balance.
MARGIN_TYPE = "cross"  # cross-margin: simpler setup, whole balance as collateral

# ── Short borrow cost ──────────────────────────────────────────────────────────
# Hourly interest rate for margin borrowing (Binance cross-margin BTC ≈ 0.0009%/h).
# Deducted from short PnL in backtesting. Verify current rate on Binance margin page.
MARGIN_INTEREST_HOURLY = 0.000009  # 0.0009% per hour

# ── Backtest scope ─────────────────────────────────────────────────────────────
# Backtest covers TECHNICAL SIGNALS ONLY (BB, RSI, multi-TF trend bias).
# The live trader's news/sentiment layer (news_analyzer.py → _decide_action)
# is NOT simulated in backtesting. Live performance may differ when news
# triggers entries that the backtest would not produce (or skips entries
# the backtest would take).
BACKTEST_COVERS_NEWS = False  # set True if news scores are wired into backtest.run()

# ── Exchange ──────────────────────────────────────────────────────────────────
EXCHANGE = "binance"

# ── News-informed trading ─────────────────────────────────────────────────────
# Sentiment scores range from -1.0 (very bearish) to +1.0 (very bullish).
#
# Decision logic:
#   score >= NEWS_STRONG_BULL              → open long WITHOUT needing a technical signal
#   score >= NEWS_WEAK_BULL + tech signal  → open long (both agree)
#   |score| < NEWS_WEAK_BULL (neutral)     → use technical strategy only (default behaviour)
#   score <= -NEWS_WEAK_BULL               → skip new long entries
#   score <= -NEWS_STRONG_BULL             → close existing longs early / open short
#   score >= NEWS_STRONG_BULL              → close existing shorts early
#
# NEWS_REFRESH_HOURS: how often to re-fetch RSS feeds (avoids hammering sources)
NEWS_STRONG_BULL      =  0.45   # strong enough to trade on news alone
NEWS_WEAK_BULL        =  0.15   # positive but needs technical confirmation
NEWS_WEAK_BEAR        = -0.08   # skip new entries (same magnitude, negative)
NEWS_STRONG_BEAR      = -0.25   # exit existing longs early
NEWS_REFRESH_HOURS    =  4      # re-fetch news every N hours

# ── LLM Trader (Phase 1) ──────────────────────────────────────────────────────
# Provider: DeepSeek via OpenAI-compatible API.
# LLM proposes, code disposes — all risk controls remain deterministic Python.
LLM_BASE_URL           = "https://api.deepseek.com"
LLM_DECISION_MODEL     = "deepseek-v4-flash" # per-cycle decisions (fast, cheap; legacy "deepseek-chat" deprecated July 2026)
LLM_DECISION_TF        = "1h"                # LLM runs every closed 1h bar
LLM_MIN_CONFIDENCE     = 0.6                 # entries below this → forced hold
LLM_TIMEOUT            = 30                  # seconds
LLM_MAX_RETRIES        = 1                   # retry count on API failure
LLM_BRAIN_MODE         = "shadow"            # "shadow" (journal only) | "rules" (off) | "llm" (live, Phase 3)

# ── LLM Trader (Phase 2 — Reflection) ──────────────────────────────────────────
LLM_REFLECTION_MODEL    = "deepseek-v4-pro"   # stronger model for daily reflection (uses thinking mode)
LLM_REFLECTION_TIMEOUT  = 120                  # seconds — reflection prompt is heavier
PLAYBOOK_MAX_LESSONS    = 20                   # hard cap on playbook size
PLAYBOOK_MAX_WORDS      = 150                  # max words per lesson text
PLAYBOOK_MAX_EDITS_DAY  = 3                    # max edits per reflection run
PLAYBOOK_DECAY_DAYS     = 30                   # days without use before lesson flagged for decay
REFLECTION_STATS_DAYS   = 7                    # rolling stats window for reflection input
