# LLM Trader Plan — from rule-based bot to LLM decision-making

Goal: an LLM "trader brain" that makes entry/exit decisions using technical + news context,
journals every decision, reflects on outcomes daily, and improves via an evolving playbook.

**Non-negotiable principle: the LLM proposes, code disposes.** All risk controls
(stop-losses, position sizing caps, circuit breaker, exchange-side stops) remain
deterministic Python and cannot be overridden by any LLM output.

Build in phases. Do not start a phase until the previous one's acceptance criteria pass.

---

## Phase 1 — Decision engine (LLM proposes, shadow mode only)

- [ ] **1.1 New module `briefing.py`** — assembles one JSON "market briefing" per cycle:
  - Technical: current + previous values of BB position, RSI, trend biases (15m/1h/4h), distance to bands, recent volatility (ATR or stdev).
  - The rule-engine's own opinion: which of `long_entry/long_exit/short_entry/short_exit` fired (the old IF-logic becomes an input feature, not the decider).
  - News: top 5 headlines with per-article sentiment + aggregate score (already in `news_analyzer.py`).
  - Account: open position (side, entry, unrealized PnL, time held), balance, daily PnL vs circuit-breaker limit.
  - Memory: last 10 closed trades (side, reasoning summary, outcome), current `playbook.md` content.
  - No raw candle dumps — derived features only, keep the briefing under ~3,000 tokens.

- [ ] **1.2 New module `llm_trader.py`** — the decision call:
  - Provider: DeepSeek API (OpenAI-compatible). Use the `openai` Python package with `base_url="https://api.deepseek.com"`; `DEEPSEEK_API_KEY` in `.env` — add to `.env.example` as placeholder, never log it.
  - Model: `deepseek-v4-flash` for per-cycle decisions (config: `LLM_DECISION_MODEL`). Keep the client wrapper thin and provider-agnostic (single `llm_client.py` with `complete(system, user, tools) -> dict`) so the provider can be swapped via config later.
  - System prompt: experienced crypto trader persona + hard constraints ("you cannot exceed limits; when uncertain, hold; capital preservation first; you trade BTC/USDT on 1h cycles").
  - Force structured output via function calling (a single `submit_decision` function with `tool_choice` required) or strict JSON mode:
    ```json
    {
      "action": "open_long | open_short | close | hold",
      "confidence": 0.0-1.0,
      "size_multiplier": 0.0-1.0,
      "reasoning": "max 100 words",
      "invalidation_price": float | null,
      "lessons_applied": ["playbook lesson ids used, if any"]
    }
    ```
  - Validation layer in code (`validate_decision()`): action must be legal for current position state; size = `RISK_PER_TRADE × size_multiplier`, clamped to min-notional and max caps; entries require `confidence >= LLM_MIN_CONFIDENCE` (config, start 0.6); circuit breaker and existing balance checks run BEFORE any LLM call (skip the call entirely when halted — saves cost).
  - On API error/timeout (use 30s timeout, 1 retry): fall back to `hold` and log. Never trade on a failed/partial response.
  - Cadence: every closed 1h bar (config `LLM_DECISION_TF = "1h"`), not 15m — cheaper, less noise. Stop-loss checks remain the existing 60s code path, untouched.

- [ ] **1.3 Journal `journal.py`** — SQLite (`journal.db`, gitignore it):
  - Table `decisions`: id, timestamp, briefing_json, action, confidence, size_multiplier, reasoning, invalidation_price, model, prompt_tokens, completion_tokens, executed (bool).
  - Table `outcomes`: decision_id FK, entry/exit price+time, pnl_usd, pnl_pct, exit_reason, max_adverse_excursion, max_favorable_excursion.
  - Hook into existing `_close_long/_close_short` to attach outcomes.

- [ ] **1.4 Shadow mode** — `live_trader.py --brain shadow` (default):
  - Rule engine keeps trading exactly as today; LLM decisions are computed and journaled but NOT executed.
  - `compare.py` script: weekly report — LLM (simulated fills incl. fees/slippage) vs rule bot vs buy-and-hold; win rate, PF, max DD, decision agreement %.
  - Acceptance: 4+ weeks of shadow data; LLM simulated performance ≥ rule bot AND ≥ buy-and-hold net of fees before Phase 3 is allowed.

## Phase 2 — Reflection loop (the "learning from mistakes")

- [ ] **2.1 `reflect.py` — daily job** (run via cron/systemd timer at 00:15 UTC):
  - Input: yesterday's closed trades with their original reasoning + outcomes, plus 7-day rolling stats.
  - Model: `deepseek-v4-pro` (config `LLM_REFLECTION_MODEL`) — stronger model (use thinking mode), runs once/day.
  - Prompt: "For each trade: was the reasoning sound and unlucky, or flawed? Identify repeated mistakes. Propose playbook edits." Force structured output: list of `{op: add|update|remove, lesson_id, text, evidence}`.
  - Apply edits to `playbook.md` with hard caps enforced in code: max 20 lessons, max 150 words each, max 3 edits/day. Each lesson stores: id, text, created date, supporting trade ids, hit-counter.
  - `playbook.md` is data, not config — gitignore it; keep `playbook.example.md` with 2–3 seed lessons in the repo.
- [ ] **2.2 Lesson decay** — monthly, lessons not applied in any decision for 30 days get flagged; reflection job must justify keeping them or they're removed. Prevents the playbook from bloating into noise.
- [ ] **2.3 Anti-overfit guard** — reflection must not produce lessons from fewer than 3 supporting trades ("one loss ≠ a rule"). Enforce in the prompt AND reject in code (`len(evidence) >= 3`).
- [ ] **2.4 (Optional, later) similar-situation recall** — embed briefings (`voyage-3.5-lite` or local sentence-transformers), store vectors in SQLite; at decision time retrieve top-3 most similar past situations + their outcomes into the prompt ("last 3 times the setup looked like this: -1.2%, +0.8%, -2.1%").

## Phase 3 — Live cutover (only after Phase 1 acceptance passes)

- [ ] **3.1 `--brain llm` flag** — LLM decisions executed through the EXISTING order layer (`_open_long`, exchange-side stops, circuit breaker all unchanged). `--brain rules` remains available as instant fallback.
- [ ] **3.2 Extra guardrails for LLM mode**:
  - Max 3 LLM-initiated entries/day (config) — stops a confused model from churning fees.
  - Auto-demote: if LLM live performance over the last 20 trades drops below the rule bot's shadow performance, automatically switch back to `--brain rules` and alert (log + optional email/Telegram).
  - `invalidation_price`, when given and tighter than `STOP_LOSS_PCT`, becomes the stop — never wider; stop remains exchange-side.
- [ ] **3.3 Cost guard** — track token spend in journal; hard monthly budget (config `LLM_MONTHLY_BUDGET_USD`, default 10); when exceeded → revert to rules + alert.
- [ ] **3.4 web_ui** — show per-decision reasoning, confidence, playbook lessons applied, and running LLM-vs-rules comparison.

## Phase 4 — Hygiene

- [ ] Tests: `validate_decision()` (every illegal action/size rejected), journal round-trip, playbook edit caps, reflection evidence threshold, fallback-to-hold on API failure (mock the API; tests must run offline).
- [ ] `requirements.txt`: add `openai` (pinned — used as the OpenAI-compatible client for DeepSeek).
- [ ] `.gitignore`: `journal.db`, `playbook.md`.
- [ ] `SECURITY_CHECK.md`: add `DEEPSEEK_API_KEY` to the env-value scan (DeepSeek keys are `sk-...`, already caught by the token-format grep); add `journal.db`/`playbook.md` to forbidden-files table (they contain account/trade history).
- [ ] README section: architecture diagram of briefing → decision → validation → execution → journal → reflection loop.

---

## Reality checks (read before building)

1. **The learning lives in the journal/playbook loop, not the model.** Skipping Phase 2 gives you an expensive random trader with good vocabulary.
2. **LLM decisions cannot be backtested honestly** — the model has knowledge of past markets, and replaying months of hourly calls costs real money. Shadow mode (Phase 1.4) is the only honest evaluation. Do not skip the 4 weeks.
3. **Costs at the proposed cadence**: ~720 decision calls/month × ~3k in / 300 out tokens on deepseek-v4-flash ≈ under $2/month; daily deepseek-v4-pro reflection ≈ ~$1/month. DeepSeek context caching is automatic and makes the repeated system prompt + playbook prefix nearly free.
4. **Known LLM failure modes to expect in reflection review**: overconfidence after win streaks, recency bias, narrative-fitting news to price. The guardrails in 3.2 exist because these WILL happen.
5. **A $28 account cannot beat fees with frequent trading** no matter how smart the brain. The LLM's main edge at this scale is trading LESS — skipping marginal setups the rule bot would take. Judge it on that.
