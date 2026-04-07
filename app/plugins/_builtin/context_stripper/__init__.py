"""
context_stripper — builtin plugin

Strips client-injected history from ctx.request.messages before forwarding.
Different clients send different amounts of baggage. We keep only the latest
user message — the upstream provider manages its own session context.

Hook points: role.context

client_mode values:
  raw (default) — trust whatever arrives, no stripping
  librechat     — strip everything except the last user message
                  (LibreChat sends full history every turn)

Place this plugin in your role.context.plugins config where you want it to run.
It runs at the position you declare it — config order is execution order.
"""

import logging

from app.context import PipelineCtx

logger = logging.getLogger("mind_span.context_stripper")

SUPPORTED_HOOKS = ["role.context", "identity.context"]


def _strip_librechat(messages: list) -> list:
    """
    LibreChat sends its entire conversation history every turn.
    We keep only the last user message.
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


def hook(hook_point: str, ctx: PipelineCtx, config: dict) -> PipelineCtx | None:
    # Prefer config-supplied client_mode, fall back to identity ctx
    client_mode = config.get("client_mode") or ctx.identity.client_mode

    if client_mode == "raw" or client_mode not in _STRATEGIES:
        return None

    messages = ctx.request.messages
    if not messages:
        return None

    before_count = len(messages)
    stripped = _STRATEGIES[client_mode](messages)
    logger.info(f"context_stripper [{client_mode}]: {before_count} messages → {len(stripped)}")
    ctx.request.messages = stripped
    return ctx
