"""
context_stripper — builtin plugin.

Strips client-injected history from the messages array before forwarding.
Different clients send different amounts of baggage. We only want the
latest user message — the upstream provider (e.g. OpenClaw) manages
its own session context.

Activated when CLIENT_MODE is set. Each client mode has its own strategy.

CLIENT_MODE=librechat:
  LibreChat sends full conversation history every turn.
  Strip everything except the last user message.

CLIENT_MODE=raw (default/unset):
  Trust whatever arrives. No stripping.

Hooks registered:
  process_request (filter, priority=5) — runs before forwarding
"""

import logging
import os

from app import hooks

logger = logging.getLogger("mind_span.context_stripper")

CLIENT_MODE = os.getenv("CLIENT_MODE", "raw").lower()


def _strip_librechat(messages: list) -> list:
    """
    LibreChat sends its entire conversation history every turn.
    We only want the last user message — OpenClaw handles the rest.
    """
    # Find the last user message
    for msg in reversed(messages):
        if msg.get("role") == "user":
            logger.debug(f"context_stripper [librechat]: stripped {len(messages)} messages → 1")
            return [msg]
    # Fallback: return as-is if no user message found
    logger.warning("context_stripper [librechat]: no user message found, passing through unchanged")
    return messages


_STRATEGIES = {
    "librechat": _strip_librechat,
}


@hooks.filter_hook("process_request", priority=5)
def strip_context(body: dict, headers: dict, ctx=None) -> dict:
    client_mode = headers.get("x-bridge-client-mode", CLIENT_MODE)
    if client_mode == "raw" or client_mode not in _STRATEGIES:
        return body

    messages = body.get("messages", [])
    if not messages:
        return body

    stripped = _STRATEGIES[client_mode](messages)
    logger.info(f"context_stripper [{client_mode}]: {len(messages)} messages → {len(stripped)}")

    return {**body, "messages": stripped}
