"""
Core pipeline — OpenAI-compat request in, LLM response out.

Hooks fired (in order):
  Actions:
    before_request(raw_body: dict, headers: dict)
    after_response(raw_body: dict, response: dict)

  Filters:
    process_request(raw_body: dict, headers: dict) → raw_body
      Use to strip/inject context before forwarding to LLM.

All hook processing happens here. Plugins register into hooks — pipeline
knows nothing about what's registered.
"""

import logging
import os
from typing import AsyncIterator

import httpx
from starlette.responses import StreamingResponse

from . import hooks
from .nonce import NONCE, NONCE_HEADER

logger = logging.getLogger(__name__)

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")


async def process(raw_body: dict, headers: dict, ctx=None):
    """Returns either a dict (non-streaming) or a StreamingResponse."""

    # 1. Notify plugins: request arrived
    await hooks.fire("before_request", raw_body, headers)

    # Inject per-user client_mode for plugins (falls back to env var in plugins if absent)
    if ctx:
        headers = {**headers, "x-bridge-client-mode": ctx.client_mode}

    # 2. Let plugins transform the request (passthrough: no-op by default)
    # ctx is passed as a keyword arg so plugins can gate on role/session config
    body = await hooks.apply("process_request", raw_body, headers, ctx=ctx)

    is_streaming = body.get("stream", False)

    if is_streaming:
        return await _forward_stream(body, headers, ctx)
    else:
        response = await _forward(body, headers, ctx)
        await hooks.fire("after_response", body, response)
        return response


def _build_forward_headers(headers: dict, ctx=None) -> dict:
    """
    Pass through all headers transparently, except:
    - authorization    → replaced with our own bearer token
    - content-length   → recalculated by httpx (body may have been transformed)
    - host             → must be upstream host, not client's
    - transfer-encoding, connection, keep-alive → hop-by-hop, breaks proxies
    - cookie           → security: don't leak client session cookies upstream
    """
    _STRIP = {
        "authorization", "content-length", "host",
        "transfer-encoding", "connection", "keep-alive", "cookie",
    }
    passthrough = {
        k: v for k, v in headers.items()
        if k.lower() not in _STRIP
    }
    token = ctx.endpoint_token if ctx else LLM_API_KEY
    forward = {
        **passthrough,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        NONCE_HEADER: NONCE,  # loopback detection
    }
    if ctx and ctx.session_key:
        forward["x-openclaw-session-key"] = ctx.session_key
    return forward


async def _forward(body: dict, headers: dict, ctx=None) -> dict:
    """Non-streaming forward — returns parsed JSON dict."""
    forward_headers = _build_forward_headers(headers, ctx)
    url = ctx.endpoint_url if ctx else LLM_BASE_URL

    if ctx and ctx.endpoint_model:
        body = {**body, "model": ctx.endpoint_model}

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{url}/chat/completions",
            json=body,
            headers=forward_headers,
        )
        logger.info(f"LLM response: status={r.status_code} content-type={r.headers.get('content-type')} body_len={len(r.content)}")
        logger.debug(f"LLM raw response body: {r.text[:2000]}")
        r.raise_for_status()
        return r.json()


async def _forward_stream(body: dict, headers: dict, ctx=None) -> StreamingResponse:
    """Streaming forward — proxies SSE straight back to client."""
    forward_headers = _build_forward_headers(headers, ctx)
    url = ctx.endpoint_url if ctx else LLM_BASE_URL

    if ctx and ctx.endpoint_model:
        body = {**body, "model": ctx.endpoint_model}

    async def stream_generator() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{url}/chat/completions",
                json=body,
                headers=forward_headers,
            ) as r:
                logger.info(f"LLM stream: status={r.status_code} content-type={r.headers.get('content-type')}")
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
