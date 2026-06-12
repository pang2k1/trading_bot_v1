"""
daily_report.py
───────────────
Emails a daily status digest of the trading bot to the owner's inbox.
Designed to be read by both a human and an AI monitoring assistant.

Runs via cron once a day. Gathers everything locally (no Binance API calls):
  - systemd service health (trading-bot, trading-bot-ui, trading-optimizer)
  - last 24h log summary: errors, warnings, last cycle line
  - today's closed trades + PnL from trades_log.csv
  - LLM shadow stats from journal.db: total decisions, last 24h count,
    action breakdown, avg confidence, last 3 decisions with reasoning
  - playbook lesson count (playbook.md)
  - disk free

Email setup (.env):
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (Google Account → Security →
                                            2-Step Verification → App passwords)
The app password is used for SMTP only and can be revoked anytime.

Usage:
  python daily_report.py            # send the email
  python daily_report.py --print    # print to stdout instead (no email)
"""

import argparse
import csv
import os
import smtplib
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

BOT_DIR     = Path(__file__).parent
LOG_FILE    = BOT_DIR / "live_trader.log"
TRADES_FILE = BOT_DIR / "trades_log.csv"
JOURNAL_DB  = BOT_DIR / "journal.db"
PLAYBOOK    = BOT_DIR / "playbook.md"

SERVICES = ["trading-bot", "trading-bot-ui", "trading-optimizer"]


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as exc:
        return f"(failed: {exc})"


def service_health() -> list[str]:
    lines = []
    for svc in SERVICES:
        state = _run(["systemctl", "is-active", svc]) or "unknown"
        mark = "OK " if state == "active" else "!! "
        lines.append(f"{mark}{svc}: {state}")
    return lines


def log_summary(hours: int = 24) -> list[str]:
    if not LOG_FILE.exists():
        return ["!! live_trader.log not found"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    errors, warnings, last_line = [], [], ""
    try:
        # Only read the tail — the log rotates at 5 MB so this is bounded anyway
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines()[-3000:]:
            ts_str = line[:19]
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts < cutoff:
                continue
            if "  ERROR" in line:
                errors.append(line)
            elif "  WARNING" in line:
                warnings.append(line)
            last_line = line
    except Exception as exc:
        return [f"!! could not parse log: {exc}"]

    out = [f"errors last {hours}h: {len(errors)}", f"warnings last {hours}h: {len(warnings)}"]
    out += [f"  ERR: {e[:200]}" for e in errors[-5:]]
    # warnings are common (news feed 403s etc.) — show only the last 3
    out += [f"  WRN: {w[:200]}" for w in warnings[-3:]]
    if last_line:
        out.append(f"last log line: {last_line[:200]}")
    return out


def trades_summary(hours: int = 24) -> list[str]:
    if not TRADES_FILE.exists():
        return ["no trades_log.csv yet (no closed trades)"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    try:
        with open(TRADES_FILE, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    exit_ts = datetime.fromisoformat(row["exit_time"])
                    if exit_ts.tzinfo is None:
                        exit_ts = exit_ts.replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    continue
                if exit_ts >= cutoff:
                    rows.append(row)
    except Exception as exc:
        return [f"!! could not read trades_log.csv: {exc}"]

    if not rows:
        return [f"closed trades last {hours}h: 0"]
    pnl = sum(float(r.get("pnl_usd") or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("pnl_usd") or 0) > 0)
    out = [f"closed trades last {hours}h: {len(rows)}  wins: {wins}  net PnL: {pnl:+.4f} USDT"]
    for r in rows[-5:]:
        out.append(
            f"  {r.get('side','?'):5s} exit={float(r.get('exit_price') or 0):.1f} "
            f"pnl={float(r.get('pnl_usd') or 0):+.4f} reason={r.get('reason','')[:40]}"
        )
    return out


def llm_summary(hours: int = 24) -> list[str]:
    if not JOURNAL_DB.exists():
        return ["!! journal.db not found — LLM shadow not recording"]
    try:
        con = sqlite3.connect(f"file:{JOURNAL_DB}?mode=ro", uri=True)
        cur = con.cursor()
        total = cur.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        recent = cur.execute(
            "SELECT COUNT(*) FROM decisions WHERE timestamp >= ?", (cutoff,)
        ).fetchone()[0]
        actions = cur.execute(
            "SELECT action, COUNT(*) FROM decisions WHERE timestamp >= ? GROUP BY action",
            (cutoff,),
        ).fetchall()
        avg_conf = cur.execute(
            "SELECT AVG(confidence) FROM decisions WHERE timestamp >= ?", (cutoff,)
        ).fetchone()[0]
        last3 = cur.execute(
            "SELECT timestamp, action, confidence, reasoning FROM decisions "
            "ORDER BY id DESC LIMIT 3"
        ).fetchall()
        tokens = cur.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM decisions"
        ).fetchone()
        con.close()
    except Exception as exc:
        return [f"!! journal.db read error: {exc}"]

    breakdown = "  ".join(f"{a}:{n}" for a, n in actions) or "none"
    out = [
        f"decisions last {hours}h: {recent}  (total: {total})",
        f"actions: {breakdown}   avg confidence: {avg_conf:.2f}" if avg_conf is not None
        else f"actions: {breakdown}",
        f"tokens all-time: {tokens[0]:,} in / {tokens[1]:,} out",
        "last decisions:",
    ]
    for ts, action, conf, reasoning in last3:
        out.append(f"  {str(ts)[:16]}  {action:11s} conf={conf:.2f}  {str(reasoning)[:90]}")
    return out


def playbook_summary() -> list[str]:
    if not PLAYBOOK.exists():
        return ["playbook.md: not created yet (reflection hasn't added lessons)"]
    text = PLAYBOOK.read_text(encoding="utf-8", errors="replace")
    lessons = sum(1 for line in text.splitlines() if line.strip().startswith("## L"))
    return [f"playbook lessons: {lessons}"]


def build_report() -> str:
    now = datetime.now(timezone.utc)
    sections = [
        ("SERVICES", service_health()),
        ("LOG (24h)", log_summary()),
        ("REAL TRADES — rule bot (24h)", trades_summary()),
        ("LLM SHADOW (24h)", llm_summary()),
        ("PLAYBOOK", playbook_summary()),
        ("DISK", [_run(["df", "-h", "/"]).splitlines()[-1] if _run(["df", "-h", "/"]) else "?"]),
    ]
    lines = [f"Trading bot daily report — {now.strftime('%Y-%m-%d %H:%M UTC')}", "=" * 60]
    for title, body in sections:
        lines.append(f"\n[{title}]")
        lines.extend(body)
    lines.append("\n(generated by daily_report.py — shadow phase ends ~2026-07-10)")
    return "\n".join(lines)


def send_email(report: str) -> None:
    load_dotenv()
    addr = os.getenv("GMAIL_ADDRESS", "")
    app_pw = os.getenv("GMAIL_APP_PASSWORD", "")
    if not addr or not app_pw:
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")

    msg = EmailMessage()
    msg["Subject"] = f"[trading-bot] Daily report {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    msg["From"] = addr
    msg["To"] = addr
    msg.set_content(report)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(addr, app_pw)
        smtp.send_message(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily trading bot status email")
    parser.add_argument("--print", action="store_true", help="Print report, don't email")
    args = parser.parse_args()

    report = build_report()
    if args.print:
        print(report)
        return
    send_email(report)
    print("Report sent.")


if __name__ == "__main__":
    main()
