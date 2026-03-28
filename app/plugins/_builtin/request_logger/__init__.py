"""
request_logger — builtin plugin.

Logs raw request and response to stdout + optional log file.
This is the passthrough observation tool — see exactly what
LibreChat/OpenClaw sends and what comes back, before any processing.

Hooks registered:
  before_request (action, priority=1)  — log incoming request
  after_response (action, priority=99) — log outgoing response
"""

import json
import logging
import os
from datetime import datetime, timezone

from app import hooks

logger = logging.getLogger("mind_span.request_logger")

LOG_FILE = os.getenv("REQUEST_LOG_FILE", "/app/logs/requests.jsonl")
LOG_REQUESTS = os.getenv("LOG_REQUESTS", "true").lower() == "true"


def _write_log(entry: dict):
    """Append a JSON line to the log file."""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"Could not write log file: {e}")


@hooks.on("before_request", priority=1)
def log_request(raw_body: dict, headers: dict):
    if not LOG_REQUESTS:
        return

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": "inbound",
        "model": raw_body.get("model"),
        "message_count": len(raw_body.get("messages", [])),
        "headers": {
            k: v for k, v in headers.items()
            if k.lower() not in ("authorization", "cookie")  # strip secrets
        },
        "body": raw_body,
    }

    logger.info(
        f"→ INBOUND | model={entry['model']} | messages={entry['message_count']}"
    )
    _write_log(entry)


@hooks.on("after_response", priority=99)
def log_response(request_body: dict, response: dict):
    if not LOG_REQUESTS:
        return

    choices = response.get("choices", [])
    reply_preview = ""
    if choices:
        reply_preview = choices[0].get("message", {}).get("content", "")[:200]

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "direction": "outbound",
        "model": response.get("model"),
        "usage": response.get("usage"),
        "reply_preview": reply_preview,
        "response": response,
    }

    logger.info(
        f"← OUTBOUND | model={entry['model']} | usage={entry['usage']}"
    )
    _write_log(entry)
