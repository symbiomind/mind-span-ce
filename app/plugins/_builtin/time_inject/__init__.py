"""
time_inject — builtin plugin

Injects current local time into bridge_context.
Solves UTC confusion for agents that need to know the correct local time.

Hook points: identity.context, role.context

Config (in context.plugins.time_inject):
  timezone: "Australia/Adelaide"   # any IANA timezone (default: UTC)

Output in bridge_context:
  current_time: "Wednesday, 1st April 2026 - 09:14 AM ACDT"
"""

import logging
from datetime import datetime

from app.context import PipelineCtx

logger = logging.getLogger("mind_span.time_inject")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

SUPPORTED_HOOKS = ["role.context", "identity.context"]


def hook(hook_point: str, ctx: PipelineCtx, config: dict) -> PipelineCtx | None:
    tz_name = config.get("timezone", "UTC")

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(f"time_inject: unknown timezone '{tz_name}', falling back to UTC")
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)

    day = now.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    friendly = now.strftime(f"%A, {day}{suffix} %B %Y - %I:%M %p %Z")

    ctx.bridge_context["current_time"] = friendly
    return ctx
