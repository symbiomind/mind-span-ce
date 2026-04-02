"""
context_stripper — builtin plugin

Strips client-injected history from ctx["request"]["messages"] before forwarding.
Different clients send different amounts of baggage. We keep only the latest
user message — the upstream provider manages its own session context.

Hook points: role.context
Auto-dispatched by pipeline when client_mode != "raw" (no config declaration needed).

client_mode values:
  raw (default) — trust whatever arrives, no stripping
  librechat     — strip everything except the last user message
                  (LibreChat sends full history every turn)

Note: context_stripper runs BEFORE bridge_context injection in the pipeline.
Strip first (reduce 91 messages to 1), then inject bridge_context into that 1 message.
This ensures bridge_context always ends up in the message that gets forwarded.
"""

import logging

logger = logging.getLogger("mind_span.context_stripper")

SUPPORTED_HOOKS = ["role.context"]


def _strip_librechat(messages: list) -> list:
    """
    LibreChat sends its entire conversation history every turn.
    We keep only the last user message (which already has bridge_context prepended).
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            logger.debug(f"context_stripper [librechat]: {len(messages)} messages → 1")
            return [msg]
    logger.warning("context_stripper [librechat]: no user message found, passing through unchanged")
    return messages


_STRATEGIES = {
    "librechat": _strip_librechat,
}


def hook(hook_point: str, ctx: dict, config: dict) -> dict | None:
    # Prefer config-supplied client_mode, fall back to identity ctx
    client_mode = config.get("client_mode") or ctx.get("identity", {}).get("client_mode", "raw")

    if client_mode == "raw" or client_mode not in _STRATEGIES:
        return None

    messages = ctx["request"]["messages"]
    if not messages:
        return None

    stripped = _STRATEGIES[client_mode](messages)
    logger.info(f"context_stripper [{client_mode}]: {len(messages)} messages → {len(stripped)}")
    ctx["request"]["messages"] = stripped
    return ctx
