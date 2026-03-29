"""
Auth dependency for mind-span-ce.

Resolves the incoming Bearer token to a RequestContext.
If config is not loaded (zero-config mode), always returns None.
If config is loaded and the token is not recognised, raises HTTP 401.
"""

import logging

from fastapi import HTTPException, Request

from .config import RequestContext, get_context_for_token, is_config_loaded

logger = logging.getLogger(__name__)


async def get_request_ctx(request: Request) -> RequestContext | None:
    """FastAPI dependency. Returns RequestContext or None (→ .env fallback)."""

    # Zero-config mode: no config.yml loaded → pass through
    if not is_config_loaded():
        return None

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")

    token = auth_header[len("bearer "):].strip()
    ctx = get_context_for_token(token)
    if ctx is None:
        logger.warning("Rejected request: unrecognised token.")
        raise HTTPException(status_code=401, detail="Unauthorized.")

    return ctx
