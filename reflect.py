"""
reflect.py
──────────
Daily reflection job for the LLM trader.

Reviews yesterday's closed trades, identifies mistakes, and proposes
playbook edits. Runs via cron at 00:15 UTC (or manually with --dry-run).

Usage:
    python reflect.py                 # normal daily run
    python reflect.py --dry-run       # preview without writing
    python reflect.py --decay-only    # only lesson decay, skip LLM
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
import llm_client
import journal
from playbook import (
    Lesson,
    apply_edits,
    get_playbook_path,
    load_lessons,
    save_lessons,
)

log = logging.getLogger(__name__)

# ── Structured output tool for reflection ─────────────────────────────────────

SUBMIT_REFLECTION_TOOL = {
    "name": "submit_reflection",
    "description": "Submit your trade analysis and proposed playbook edits.",
    "parameters": {
        "type": "object",
        "properties": {
            "trade_reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "integer"},
                        "verdict": {
                            "type": "string",
                            "enum": ["sound_unlucky", "flawed"],
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": ["decision_id", "verdict", "explanation"],
                },
            },
            "repeated_mistakes": {
                "type": "array",
                "items": {"type": "string"},
            },
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["add", "update", "remove"]},
                        "lesson_id": {"type": "string"},
                        "text": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["op", "lesson_id", "text", "evidence"],
                },
            },
        },
        "required": ["trade_reviews", "repeated_mistakes", "edits"],
    },
}

REFLECTION_SYSTEM_PROMPT = """You are a trading performance analyst reviewing yesterday's trades and
recent performance data. Your job is to identify flawed reasoning, repeated mistakes, and propose
edits to the trading playbook.

Hard constraints you CANNOT override:
- Every proposed lesson MUST be supported by at least 3 distinct trades as evidence. One loss is not a rule.
- Each lesson must be 150 words or fewer.
- You may propose at most 3 edits per day.
- Do not remove seed lessons (those with evidence "none").

Analysis framework:
1. For each trade: was the reasoning sound and the outcome unlucky, or was the reasoning itself flawed?
2. Look for patterns: are the same mistakes repeating? Are certain conditions consistently misjudged?
3. Propose specific, actionable edits to the playbook. Each edit must cite the decision IDs that
   support it.

Playbook edit types:
- add: Create a new lesson. Must cite >= 3 supporting decision IDs as evidence.
- update: Modify an existing lesson with new evidence or refined text. Must cite >= 3 decision IDs.
- remove: Remove a lesson that has proven unhelpful. Provide the lesson ID and reason.

Output: call submit_reflection with your analysis."""


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_reflection_prompt(
    trades: list[dict],
    stats: dict,
    lessons: list[Lesson],
    recent_reflections: list[dict],
) -> str:
    """Build the user message for the reflection LLM call."""
    # Summarise trades — don't dump full briefing_json
    trade_summaries = []
    for t in trades:
        trade_summaries.append({
            "decision_id": t["decision_id"],
            "action": t["action"],
            "confidence": t.get("confidence"),
            "reasoning": t.get("reasoning", ""),
            "lessons_applied": _parse_json_list(t.get("lessons_applied_json", "[]")),
            "outcome": {
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "pnl_usd": t.get("pnl_usd"),
                "pnl_pct": t.get("pnl_pct"),
                "exit_reason": t.get("exit_reason"),
            },
        })

    playbook_summary = [
        {"id": l.id, "text": l.text[:200], "hits": l.hits, "evidence": l.evidence}
        for l in lessons
    ]

    recent = []
    for r in recent_reflections[:5]:
        recent.append({
            "date": r.get("timestamp", "")[:10],
            "edits_applied": r.get("edits_applied", 0),
            "edits_rejected": r.get("edits_rejected", 0),
        })

    return json.dumps({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "yesterday_trades": trade_summaries,
        "rolling_stats": stats,
        "current_playbook": playbook_summary,
        "recent_reflections": recent,
    }, indent=2, ensure_ascii=False)


# ── Response parsing ─────────────────────────────────────────────────────────

def parse_reflection_response(response: dict) -> dict:
    """Extract reflection data from LLM response. Tool call or JSON fallback."""
    tool_calls = response.get("tool_calls", [])
    if tool_calls:
        args = tool_calls[0].get("arguments", "{}")
        if isinstance(args, str):
            args = json.loads(args)
        return args

    content = response.get("content", "")
    if content:
        # Try to find JSON in the content
        start = content.find("{")
        if start >= 0:
            return json.loads(content[start:])
    raise ValueError("No structured response from reflection LLM")


# ── Lesson decay ─────────────────────────────────────────────────────────────

def apply_decay(
    lessons: list[Lesson],
    hit_counts: dict[str, int],
) -> tuple[list[Lesson], list[str]]:
    """Remove lessons with 0 hits in the decay window. Seed lessons are exempt."""
    surviving: list[Lesson] = []
    removed: list[str] = []

    for lesson in lessons:
        # Seed lessons (evidence=['none'] or empty) are never decayed
        if not lesson.evidence or lesson.evidence == ["none"]:
            surviving.append(lesson)
            continue

        hits = hit_counts.get(lesson.id, 0)
        if hits == 0:
            log.info(
                f"Decay: removing lesson {lesson.id} "
                f"(0 hits in {config.PLAYBOOK_DECAY_DAYS} days)"
            )
            removed.append(lesson.id)
        else:
            surviving.append(lesson)

    return surviving, removed


# ── Main reflection logic ────────────────────────────────────────────────────

def run_reflection(
    dry_run: bool = False,
    playbook_path: Path | None = None,
) -> dict:
    """
    Run the daily reflection job.

    Returns dict with: trades_reviewed, edits_proposed, edits_applied,
    edits_rejected, decayed, model, error.
    """
    result = {
        "trades_reviewed": 0,
        "edits_proposed": 0,
        "edits_applied": 0,
        "edits_rejected": 0,
        "decayed": [],
        "model": "",
        "error": None,
    }

    # 1. Fetch data
    trades = journal.get_yesterday_closed_trades()
    result["trades_reviewed"] = len(trades)

    # Run decay regardless of trade count
    lessons = load_lessons(playbook_path)
    hit_counts = journal.get_lesson_hit_counts(since_days=config.PLAYBOOK_DECAY_DAYS)
    lessons, decayed = apply_decay(lessons, hit_counts)
    result["decayed"] = decayed

    if decayed:
        log.info(f"Decay: removing {len(decayed)} unused lesson(s): {decayed}")
        if not dry_run:
            save_lessons(lessons, playbook_path)

    if not trades:
        log.info("No trades yesterday — skipping LLM reflection.")
        return result

    # 2. Prepare prompt
    stats = journal.get_rolling_stats(days=config.REFLECTION_STATS_DAYS)
    recent = journal.get_recent_reflections(limit=5)

    # Reload lessons after decay save
    lessons = load_lessons(playbook_path)
    prompt = build_reflection_prompt(trades, stats, lessons, recent)

    # 3. Call LLM with thinking mode
    try:
        response = llm_client.complete(
            system=REFLECTION_SYSTEM_PROMPT,
            user=prompt,
            tools=[SUBMIT_REFLECTION_TOOL],
            thinking=True,
            model=config.LLM_REFLECTION_MODEL,
        )
    except Exception as exc:
        log.error(f"Reflection LLM call failed: {exc}")
        result["error"] = str(exc)
        journal.record_reflection(
            edits=[], trades_reviewed=len(trades),
            edits_applied=0, edits_rejected=0,
            model="error", prompt_tokens=0, completion_tokens=0,
        )
        return result

    # 4. Parse response
    try:
        parsed = parse_reflection_response(response)
    except Exception as exc:
        log.error(f"Reflection response parsing failed: {exc}")
        result["error"] = str(exc)
        return result

    edits = parsed.get("edits", [])
    result["edits_proposed"] = len(edits)

    # Log trade reviews
    for review in parsed.get("trade_reviews", []):
        log.info(
            f"[reflect] Trade {review.get('decision_id')}: "
            f"{review.get('verdict')} — {review.get('explanation', '')[:100]}"
        )
    for mistake in parsed.get("repeated_mistakes", []):
        log.info(f"[reflect] Repeated mistake: {mistake[:120]}")

    # 5. Validate and apply edits
    new_lessons, applied, rejected = apply_edits(lessons, edits)
    result["edits_applied"] = len(applied)
    result["edits_rejected"] = len(rejected)

    for a in applied:
        log.info(f"[reflect] Applied: {a['op']} {a.get('lesson_id', '(new)')}")
    for r in rejected:
        log.warning(f"[reflect] Rejected edit: {r}")

    # 6. Save
    if not dry_run:
        save_lessons(new_lessons, playbook_path)
        journal.record_reflection(
            edits=edits,
            trades_reviewed=len(trades),
            edits_applied=len(applied),
            edits_rejected=len(rejected),
            model=response.get("model", ""),
            prompt_tokens=response.get("prompt_tokens", 0),
            completion_tokens=response.get("completion_tokens", 0),
        )
    else:
        log.info("[dry-run] No changes written.")

    result["model"] = response.get("model", "")
    log.info(
        f"[reflect] Done: {len(trades)} trades reviewed, "
        f"{len(applied)} edits applied, {len(rejected)} rejected, "
        f"{len(decayed)} decayed."
    )
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description="LLM Trader Daily Reflection")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--decay-only", action="store_true", help="Only run lesson decay, skip LLM")
    args = parser.parse_args()

    if args.decay_only:
        lessons = load_lessons()
        hit_counts = journal.get_lesson_hit_counts(since_days=config.PLAYBOOK_DECAY_DAYS)
        lessons, decayed = apply_decay(lessons, hit_counts)
        if decayed:
            save_lessons(lessons)
            log.info(f"Decay: removed {len(decayed)} lesson(s): {decayed}")
        else:
            log.info("Decay: no lessons to remove.")
        return

    result = run_reflection(dry_run=args.dry_run)
    if result.get("error"):
        log.error(f"Reflection failed: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_json_list(raw: str) -> list:
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
