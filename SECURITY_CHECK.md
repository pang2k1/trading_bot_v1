# Pre-Commit Security Checklist — trading_bot_v1

**MANDATORY: Read and execute this entire checklist before EVERY commit. Run every command. If ANY check fails, STOP — do not commit, do not push. Report the failure and fix it first. Never use `git add .` or `git add -A` without completing this checklist.**

This repo controls real money via Binance API keys. A leaked key = stolen funds.

---

## 1. Hard rules — files that must NEVER be committed

| File / pattern | Why |
|---|---|
| `.env` (any variant: `.env.live`, `.env.backup`, `.env.old`) | Binance API keys, web UI password |
| `trader_state.json`, `trades_log.csv`, `live_trader.log`, `*.log` | Account balances, positions, trade history |
| `best_params.json`, `optimization_results.json`, `news_report.json` | Runtime artifacts |
| `journal.db`, `playbook.md` | LLM trade history, evolving trading lessons — contain account data |
| `.claude/settings.local.json` | Machine-specific settings |
| `venv/`, `data/*.parquet`, `*.csv` data caches | Bloat; caches may embed account data |
| Any file containing a key, secret, token, password, seed phrase, or private key | Obvious |

`.env.example` IS allowed — but verify it contains only placeholder values (check #4).

## 2. Audit exactly what is staged

```bash
git status
git diff --cached --stat
git diff --cached            # read the full diff — every line
```

- [ ] Every staged file is one I intend to commit.
- [ ] No file from the table in section 1 is staged:

```bash
git diff --cached --name-only | grep -Ei '(^|/)\.env|trader_state|trades_log|\.log$|best_params|optimization_results|news_report|settings\.local' && echo "FAIL: forbidden file staged" || echo "OK"
```

- [ ] Nothing forbidden is already tracked from a past commit:

```bash
git ls-files | grep -Ei '(^|/)\.env$|\.env\.|trader_state|trades_log|\.log$|best_params|optimization_results|news_report|settings\.local' && echo "FAIL: forbidden file is TRACKED — see section 7" || echo "OK"
```

## 3. Scan staged content for secrets

Run each; all must return no matches (exit non-zero):

```bash
# Binance-style API keys (64-char alphanumeric) and generic long tokens
git diff --cached | grep -E '[A-Za-z0-9]{64}' && echo "FAIL: possible API key"

# Assignments of secrets with non-empty literal values
git diff --cached | grep -nEi '(api[_-]?key|api[_-]?secret|secret|token|passw(or)?d|private[_-]?key|seed[_-]?phrase|mnemonic)\s*[=:]\s*["'"'"'][^"'"'"']{8,}' && echo "FAIL: hardcoded credential"

# Common token formats (AWS, GitHub, Slack, Telegram, OpenAI/Anthropic, JWT)
git diff --cached | grep -nE '(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|github_pat_|xox[bporas]-|sk-[A-Za-z0-9]{20,}|sk-ant-|eyJ[A-Za-z0-9_-]{20,}\.eyJ|[0-9]{8,10}:[A-Za-z0-9_-]{35})' && echo "FAIL: token detected"

# .env values accidentally pasted into code/docs
git diff --cached | grep -nE '(TESTNET_API_KEY|TESTNET_SECRET|LIVE_API_KEY|LIVE_SECRET|WEB_UI_PASSWORD|DEEPSEEK_API_KEY)\s*=\s*[^"\s]{4,}' && echo "FAIL: env value in diff"
```

- [ ] All four scans clean.
- [ ] If a scanner like `gitleaks` or `trufflehog` is installed, run it too: `gitleaks protect --staged -v`. (Recommended: install gitleaks.)

## 4. Project-specific code checks (on staged Python/HTML/JS)

- [ ] No credentials hardcoded as fallbacks. The only acceptable pattern is reading from env, e.g. `os.getenv("LIVE_API_KEY", "")`. Flag any `os.getenv(..., "<real-looking default>")`.
- [ ] `web_ui.py`: default password `"changeme"` must never become a working login path; startup must still refuse to run with unset/default password.
- [ ] No real values in `.env.example` — placeholders only (`your_key_here`):

```bash
grep -E '=\s*\S{20,}' .env.example && echo "FAIL: .env.example may contain real values" || echo "OK"
```

- [ ] No personal data in committed docs/code: real account balances, order IDs, server IPs, home paths with username (`/Users/...`) in anything staged:

```bash
git diff --cached | grep -nE '(/Users/[a-z0-9_.-]+|/home/[a-z0-9_.-]+|([0-9]{1,3}\.){3}[0-9]{1,3})' | grep -v '0\.0\.0\.0\|127\.0\.0\.1' && echo "REVIEW: paths/IPs found — confirm not sensitive"
```

- [ ] No debug prints/logging of secrets added (`print(api_key)`, `log.info(f"...{secret}...")`).
- [ ] Notebooks (`.ipynb`): outputs cleared if any are staged.

## 5. Dependency & config sanity

- [ ] No new dependency added from an unknown/typosquatted package name (verify spelling on PyPI before committing changes to `requirements.txt`).
- [ ] `.gitignore` still contains: `.env`, `*.log`, `trader_state.json`, `trades_log.csv`, `best_params.json`, `optimization_results.json`, `news_report.json`, `journal.db`, `playbook.md`, `venv/`. If new runtime/data files were introduced in this change (e.g. `data/` cache, pidfile), add them to `.gitignore` in the same commit.

## 6. Final gate

- [ ] Sections 1–5 all pass.
- [ ] Commit message contains no secrets, keys, balances, or internal IPs.
- [ ] I re-ran `git status` after any fix and re-checked what is staged.

Only after every box is checked: commit. Otherwise: unstage (`git restore --staged <file>`), fix, and restart this checklist.

## 7. If a secret was ever committed (now or in history)

A secret in ANY past commit is compromised even if deleted later.

1. **Rotate immediately**: delete the exposed Binance API key in Binance account settings and create a new one (do this FIRST, before fixing git). Same for any other leaked credential.
2. Remove the file from tracking: `git rm --cached <file>` + ensure it's in `.gitignore`.
3. If already pushed to GitHub: rewrite history (`git filter-repo` or BFG) and force-push, then verify on GitHub that no commit still shows the secret. Treat the key as burned regardless.
4. Check history for leaks at any time with:

```bash
git log --all -p | grep -nE '(LIVE_API_KEY|TESTNET_API_KEY|SECRET)\s*=\s*\S{10,}' | head
```
