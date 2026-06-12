"""
llm_trader.py
─────────────
LLM decision engine — the "trader brain".

Builds a prompt from the market briefing, calls the LLM, validates
the decision, and falls back to hold on any error.

Non-negotiable principle: the LLM proposes, code disposes.
All risk controls remain deterministic Python.
"""

import json
import logging

import config
import llm_client
import journal
from briefing import assemble_briefing, briefing_to_text

log = logging.getLogger(__name__)

# ── Structured output tool definition ─────────────────────────────────────────

SUBMIT_DECISION_TOOL = {
    "name": "submit_decision",
    "description": "Submit your trading decision with reasoning.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open_long", "open_short", "close", "hold"],
                "description": "The trading action to take.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Your confidence in this decision, 0 to 1.",
            },
            "size_multiplier": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Position size as fraction of max allowed (0=minimum, 1=full size).",
            },
            "reasoning": {
                "type": "string",
                "maxLength": 100,
                "description": "Your reasoning in 100 words or fewer.",
            },
            "invalidation_price": {
                "type": "number",
                "nullable": True,
                "description": "Price at which this thesis is invalidated (optional stop level).",
            },
            "lessons_applied": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IDs of playbook lessons that influenced this decision.",
            },
        },
        "required": ["action", "confidence", "size_multiplier", "reasoning"],
    },
}

SYSTEM_PROMPT = """You are an experienced crypto trader managing a BTC/USDT position on 1-hour cycles.

Hard constraints you CANNOT override:
- Position sizing, stop-losses, circuit breaker, and max daily loss are enforced in code.
- You cannot exceed risk limits. When uncertain, hold.
- Capital preservation is your top priority.
- You trade BTC/USDT. Your timeframe is 1h bars.

Decision framework:
1. Analyze the technical indicators (BB position, RSI, trend biases, volatility).
2. Consider news sentiment and how it aligns with technicals.
3. Review the rule engine's signals — they are a data point, not orders.
4. Check recent trade history for patterns to exploit or avoid.
5. Apply playbook lessons where relevant.

Key principles:
- Only take high-conviction setups. Missing a move is fine; taking a bad one is not.
- If the rule engine says hold and you have no strong counter-thesis, hold.
- Close means close the current position (whatever side it is).
- Size_multiplier controls how much of the allowed risk to use. Use 0.3-0.5 for marginal setups, 0.7-1.0 for strong ones.
- Always provide invalidation_price when opening a position — the price that proves your thesis wrong."""


# ── Validation ──────────────────────────────────────────────────────────────────

def validate_decision(decision: dict, current_side: str | None) -> dict:
    """
    Validate an LLM decision against current state.

    Returns the (possibly corrected) decision dict.
    Raises ValueError for fundamentally invalid decisions.
    """
    action = decision.get("action", "hold")

    # Valid actions
    valid_actions = {"open_long", "open_short", "close", "hold"}
    if action not in valid_actions:
        log.warning(f"[llm] Invalid action '{action}' — falling back to hold.")
        decision["action"] = "hold"
        decision["reasoning"] = f"Invalid action '{action}' corrected to hold."
        return decision

    # Check action legality against position state
    if action == "open_long" and current_side == "long":
        log.warning("[llm] Cannot open long — already long. Falling back to hold.")
        decision["action"] = "hold"
        decision["reasoning"] = "Already long; cannot open another."
        return decision

    if action == "open_short" and current_side == "short":
        log.warning("[llm] Cannot open short — already short. Falling back to hold.")
        decision["action"] = "hold"
        decision["reasoning"] = "Already short; cannot open another."
        return decision

    if action == "open_long" and current_side == "short":
        log.warning("[llm] Cannot open long — short position open. Falling back to hold.")
        decision["action"] = "hold"
        decision["reasoning"] = "Short position open; use 'close' first."
        return decision

    if action == "open_short" and current_side == "long":
        log.warning("[llm] Cannot open short — long position open. Falling back to hold.")
        decision["action"] = "hold"
        decision["reasoning"] = "Long position open; use 'close' first."
        return decision

    if action == "close" and current_side is None:
        log.warning("[llm] Cannot close — no position open. Falling back to hold.")
        decision["action"] = "hold"
        decision["reasoning"] = "No position to close."
        return decision

    # Confidence threshold
    confidence = decision.get("confidence", 0)
    min_conf = getattr(config, "LLM_MIN_CONFIDENCE", 0.6)
    if action in ("open_long", "open_short") and confidence < min_conf:
        log.info(f"[llm] Confidence {confidence:.2f} below threshold {min_conf} — holding.")
        decision["action"] = "hold"
        decision["reasoning"] = f"Confidence {confidence:.2f} below minimum {min_conf}."
        return decision

    # Clamp size_multiplier
    sm = decision.get("size_multiplier", 1.0)
    decision["size_multiplier"] = max(0.0, min(1.0, float(sm)))

    # Clamp confidence
    decision["confidence"] = max(0.0, min(1.0, float(confidence)))

    # Truncate reasoning
    reasoning = decision.get("reasoning", "")
    if len(reasoning) > 200:
        decision["reasoning"] = reasoning[:200]

    return decision


# ── Main decision function ──────────────────────────────────────────────────────

def make_decision(
    df,
    symbol: str,
    balance: float,
    state: dict | None = None,
    news_cache: dict | None = None,
    circuit_daily_pnl: float = 0.0,
    circuit_start_balance: float = 0.0,
    circuit_halted: bool = False,
) -> dict:
    """
    Build briefing, call LLM, validate, and journal the decision.

    Returns a dict with: action, confidence, size_multiplier, reasoning,
    invalidation_price, lessons_applied, decision_id, model, error.
    On any failure, returns a hold decision.
    """
    # Circuit breaker check — skip the LLM call entirely
    if circuit_halted:
        log.info("[llm] Circuit breaker active — skipping LLM call.")
        return _hold_decision(reason="Circuit breaker halted trading.")

    # Assemble briefing
    try:
        briefing = assemble_briefing(
            df=df,
            symbol=symbol,
            balance=balance,
            state=state,
            news_cache=news_cache,
            circuit_daily_pnl=circuit_daily_pnl,
            circuit_start_balance=circuit_start_balance,
        )
    except Exception as exc:
        log.error(f"[llm] Briefing assembly failed: {exc}")
        return _hold_decision(reason=f"Briefing error: {exc}")

    # Determine current position side
    current_side = None
    if state:
        for sym, pos in state.items():
            current_side = pos["side"]
            break

    # Call the LLM
    try:
        response = llm_client.complete(
            system=SYSTEM_PROMPT,
            user=briefing_to_text(briefing),
            tools=[SUBMIT_DECISION_TOOL],
        )
    except Exception as exc:
        log.error(f"[llm] API call failed: {exc} — falling back to hold.")
        return _hold_decision(reason=f"API error: {exc}")

    # Parse the structured response
    try:
        decision = _parse_response(response)
    except Exception as exc:
        log.error(f"[llm] Response parsing failed: {exc} — falling back to hold.")
        return _hold_decision(
            reason=f"Parse error: {exc}",
            briefing=briefing,
            model=response.get("model", "unknown"),
            prompt_tokens=response.get("prompt_tokens", 0),
            completion_tokens=response.get("completion_tokens", 0),
        )

    # Validate
    try:
        decision = validate_decision(decision, current_side)
    except ValueError as exc:
        log.error(f"[llm] Decision rejected: {exc} — falling back to hold.")
        decision = _hold_decision(reason=f"Validation error: {exc}")

    # Map generic 'close' to specific close action
    if decision["action"] == "close":
        if current_side == "long":
            decision["action"] = "close_long"
        elif current_side == "short":
            decision["action"] = "close_short"
        else:
            decision["action"] = "hold"

    # Journal the decision
    decision_id = 0
    try:
        decision_id = journal.record_decision(
            briefing=briefing,
            action=decision["action"],
            confidence=decision.get("confidence", 0),
            size_multiplier=decision.get("size_multiplier", 0),
            reasoning=decision.get("reasoning", ""),
            invalidation_price=decision.get("invalidation_price"),
            model=response.get("model", "unknown"),
            prompt_tokens=response.get("prompt_tokens", 0),
            completion_tokens=response.get("completion_tokens", 0),
            executed=False,  # shadow mode — not executed
            lessons_applied=decision.get("lessons_applied", []),
        )
    except Exception as exc:
        log.warning(f"[llm] Journal write failed: {exc}")

    decision["decision_id"] = decision_id
    log.info(
        f"[llm] Decision: {decision['action']}  "
        f"confidence={decision.get('confidence', 0):.2f}  "
        f"size={decision.get('size_multiplier', 0):.2f}  "
        f"reasoning={decision.get('reasoning', '')[:80]}"
    )
    return decision


def _hold_decision(
    reason: str = "",
    briefing: dict | None = None,
    model: str = "fallback",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    """Create a hold decision with journal entry."""
    decision_id = 0
    if briefing is not None:
        try:
            decision_id = journal.record_decision(
                briefing=briefing,
                action="hold",
                confidence=0,
                size_multiplier=0,
                reasoning=reason,
                invalidation_price=None,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                executed=False,
            )
        except Exception:
            pass

    return {
        "action": "hold",
        "confidence": 0,
        "size_multiplier": 0,
        "reasoning": reason,
        "invalidation_price": None,
        "lessons_applied": [],
        "decision_id": decision_id,
        "model": model,
    }


def _parse_response(response: dict) -> dict:
    """Extract the decision from the LLM's tool call response."""
    tool_calls = response.get("tool_calls", [])
    if not tool_calls:
        # Fallback: try to parse content as JSON
        content = response.get("content", "")
        if content:
            return json.loads(content)
        raise ValueError("No tool calls and empty content in LLM response")

    tc = tool_calls[0]
    args = tc.get("arguments", "{}")
    if isinstance(args, str):
        args = json.loads(args)
    return args
