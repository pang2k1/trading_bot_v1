# Project rules for Claude Code

## Committing — MANDATORY

Before ANY `git commit` (or `git push`), you MUST:

1. Read `SECURITY_CHECK.md` in full.
2. Execute every command in it and complete every checkbox.
3. If any check fails, DO NOT commit. Fix the issue and restart the checklist.

Never use `git add .` or `git add -A`. Stage files explicitly by name.
Never commit or push without being explicitly asked.

## Context

- This is a live crypto trading bot — code handles real money and Binance API keys.
- `.env` holds all secrets and must never be read into output, logged, or committed.
- Pending work items are tracked in `FIXES.md`.
