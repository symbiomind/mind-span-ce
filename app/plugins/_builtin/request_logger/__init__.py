"""
request_logger — builtin plugin

Logs requests and responses to stdout and optional JSONL log file.
Observe exactly what arrives and what leaves, before and after pipeline processing.

Hook points: server (per-request, fires early), identity (fires later with full ctx)

Config (under server.plugins.request_logger or identity.plugins.request_logger):
  enabled:   bool   — default true
  log_file:  str    — path to JSONL log file (default: env LOG_FILE or /app/logs/requests.jsonl)
  log_body:  bool   — include full request/response body in log (default: false, can be large)

Note: In v0.2 this plugin is a skeleton — full request/response logging requires
the server plugin to capture the response too. That wiring comes with OpenAI-Provider.
For now it logs what it can see at each hook point.
"""

import json
import logging
import os
from datetime import datetime, timezone

from app.context import PipelineCtx

logger = logging.getLogger("mind_span.request_logger")

SUPPORTED_HOOKS = ["server", "identity"]

_DEFAULT_LOG_FILE = os.getenv("REQUEST_LOG_FILE", "/app/logs/requests.jsonl")


def hook(hook_point: str, ctx: PipelineCtx, config: dict) -> PipelineCtx | None:
    if not config.get("enabled", True):
        return None

    log_file = config.get("log_file", _DEFAULT_LOG_FILE)
    log_body = config.get("log_body", False)

    if hook_point == "server":
        # Per-request server hook — log arrival with basic info
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "server",
            "model": ctx.request.model,
            "stream": ctx.request.stream,
            "message_count": len(ctx.request.messages),
        }
        if log_body:
            entry["body"] = ctx.request.raw_body
        logger.info(
            f"→ REQUEST | model={ctx.request.model} | messages={len(ctx.request.messages)} | stream={ctx.request.stream}"
        )
        _write_log(log_file, entry)

    elif hook_point == "identity":
        # Identity hook — we now know who called
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": "identity",
            "identity": ctx.identity.key,
            "role": ctx.role.key,
            "resource": ctx.resource.key,
        }
        logger.info(
            f"→ IDENTITY | identity={ctx.identity.key} | role={ctx.role.key} | resource={ctx.resource.key}"
        )
        _write_log(log_file, entry)

    return None  # read-only plugin — never modifies ctx


def _write_log(log_file: str, entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"request_logger: could not write to '{log_file}': {e}")
