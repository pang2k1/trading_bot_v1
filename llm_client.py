"""
llm_client.py
─────────────
Thin, provider-agnostic LLM client wrapper.

Uses the OpenAI-compatible API (works with DeepSeek via base_url).
Single entry point: complete(system, user, tools) -> dict.

Provider is swappable via config — just change base_url and model.
"""

import json
import logging
import os

from dotenv import load_dotenv

import config

log = logging.getLogger(__name__)

# Lazy singleton — created on first call
_client = None


def _get_client():
    """Lazy-init the OpenAI-compatible client."""
    global _client
    if _client is not None:
        return _client

    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key or "paste_your" in api_key:
        raise ValueError(
            "DEEPSEEK_API_KEY must be set in .env.\n"
            "Get one from https://platform.deepseek.com/"
        )

    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai package not installed. Run: pip install openai")

    _client = OpenAI(
        api_key=api_key,
        base_url=getattr(config, "LLM_BASE_URL", "https://api.deepseek.com"),
        timeout=getattr(config, "LLM_TIMEOUT", 30),
        max_retries=getattr(config, "LLM_MAX_RETRIES", 1),
    )
    return _client


def reset_client():
    """Force re-creation of the client (e.g. after config change)."""
    global _client
    _client = None


def complete(system: str, user: str, tools: list[dict] | None = None) -> dict:
    """
    Send a chat completion request and return the parsed response.

    Parameters
    ----------
    system : str
        System prompt.
    user : str
        User message (typically the briefing).
    tools : list[dict] | None
        OpenAI-style function tool definitions.

    Returns
    -------
    dict with keys:
        - "content": str (text content, if any)
        - "tool_calls": list[dict] (tool call results, if any)
        - "model": str
        - "prompt_tokens": int
        - "completion_tokens": int

    Raises
    ------
    Exception on API error (caller should catch and fallback).
    """
    client = _get_client()
    model = getattr(config, "LLM_DECISION_MODEL", "deepseek-v4-flash")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
    }
    if tools:
        kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        kwargs["tool_choice"] = "required"

    response = client.chat.completions.create(**kwargs)

    choice = response.choices[0]
    result = {
        "content": choice.message.content or "",
        "tool_calls": [],
        "model": response.model or model,
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
    }

    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            result["tool_calls"].append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })

    return result
