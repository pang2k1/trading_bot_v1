# trading_bot_v1

A BTC/USDT margin trading bot for Binance with two brains: a rule-based strategy (Bollinger Band mean reversion + RSI + multi-timeframe trend filters) that trades live, and an LLM "trader brain" (DeepSeek V4) that currently runs in shadow mode — making and journaling its own decisions every hour without executing them. After a 4-week evaluation (ends ~2026-07-10), the LLM is promoted to live trading only if it beat both the rule bot and buy-and-hold net of fees.

An interactive architecture diagram lives at `docs/architecture.html` (open in any browser, toggle high-level/detailed).

## How it works

The rule engine evaluates every closed 15m bar: BB/RSI entry signals filtered by 1h and 4h trend bias plus news sentiment (RSS headlines scored with VADER). Orders go to Binance cross-margin with exchange-side stop-losses, fixed-fraction sizing, and a 3% daily-loss circuit breaker.

In parallel, every closed 1h bar, `briefing.py` assembles a market briefing (indicators, news, account state, recent trades, playbook lessons) and `llm_trader.py` asks DeepSeek for a structured decision — action, confidence, size, reasoning. A validation layer clamps anything illegal; nothing the LLM says can bypass the risk controls. Decisions land in `journal.db`. Nightly, `reflect.py` reviews closed trades against the LLM's reasoning and maintains `playbook.md` — lessons that feed back into future briefings. Weekly, `optimizer.py` re-tunes the rule engine's parameters with walk-forward validation and the live trader hot-reloads them.

The LLM proposes, code disposes: position sizing, stops, circuit breaker, and daily caps are deterministic Python in every mode.

## Modules

| File | Role |
|---|---|
| `live_trader.py` | Main loop: signals, orders, stops, circuit breaker, LLM shadow hook |
| `strategy.py` / `indicators.py` | Rule-engine signals (boolean columns, no look-ahead) |
| `data_fetcher.py` | OHLCV download with parquet cache, higher TFs resampled from 15m |
| `news_analyzer.py` | RSS headlines → VADER sentiment per symbol |
| `backtest.py` / `main.py` | Vectorized backtest engine and CLI runner |
| `optimizer.py` | Optuna walk-forward parameter tuning (systemd, weekly + on degradation) |
| `briefing.py` / `llm_trader.py` / `llm_client.py` | LLM decision pipeline (DeepSeek, OpenAI-compatible) |
| `journal.py` / `reflect.py` / `playbook.py` | Decision journal, nightly reflection, lesson management |
| `compare.py` | LLM (simulated) vs rule bot vs buy-and-hold report |
| `web_ui.py` | FastAPI dashboard (positions, trades, logs, LLM pane), binds 127.0.0.1 |
| `daily_report.py` | Emails a daily status digest via Gmail SMTP (port 587) |
| `dashboard.py` | Generates a static architecture explainer page |

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys (never commit .env)
pytest tests/ -q            # 132 tests, all offline
```

`.env` keys: `TESTNET_API_KEY`/`TESTNET_SECRET` (paper), `LIVE_API_KEY`/`LIVE_SECRET` + `CONFIRM_LIVE_TRADING=YES` (real money), `DEEPSEEK_API_KEY` (LLM), `WEB_UI_USERNAME`/`WEB_UI_PASSWORD` (dashboard), `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` (daily report).

## Running

```bash
python main.py                        # backtest
python optimizer.py --trials 100      # parameter tuning (add --apply to save)
python live_trader.py --once          # single cycle, testnet
python live_trader.py --live          # real money (requires CONFIRM_LIVE_TRADING=YES)
python web_ui.py                      # dashboard on 127.0.0.1:8080
python compare.py                     # shadow-phase comparison report
python daily_report.py --print        # status digest (omit --print to email)
```

`--brain shadow|rules|llm` controls the LLM's role (default `shadow`: journal only, never execute). `llm` mode is gated behind the Phase 3 criteria in `LLM_TRADER_PLAN.md`.

## Production deployment

Runs on a VPS under systemd: `trading-bot` (live trader), `trading-bot-ui` (dashboard), `trading-optimizer` (scheduled tuning). Two cron jobs: `reflect.py` at 00:15 UTC, `daily_report.py` at 00:30 UTC. The dashboard is reached via Tailscale (`tailscale serve 8080`) — never exposed to the public internet. Deploys are `git pull` + `systemctl restart`.

Monitoring: the daily email digest is read each morning by a scheduled Claude task that flags anomalies (services down, errors, missing decisions, abnormal PnL); a missing email is itself the alarm. A weekly task reviews the `compare.py` report against the Phase 3 promotion criteria.

## Project docs

`LLM_TRADER_PLAN.md` — the phased LLM design and promotion gates. `FIXES.md` — the audit checklist that drove the 2026-06-12 overhaul (24 issues, including a signal-collision bug that prevented all entries, look-ahead bias, and broken short accounting). `SECURITY_CHECK.md` — mandatory pre-commit checklist (enforced via `CLAUDE.md` for Claude Code); never commit `.env`, runtime state, logs, `journal.db`, or `playbook.md`.

## Disclaimer

This bot trades real money on a small account. It is a personal experiment, not financial advice; expect losses, fees matter more than cleverness at this account size, and past backtests (even honest ones) do not predict future results.
