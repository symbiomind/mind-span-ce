"""
Core pipeline — OpenAI-compat request in, LLM response out.

Pipeline flow (per request):
  1. Build ctx dict from IdentityContext + raw request body + headers
  2. Dispatch identity_context_plugins at hook "identity.context"
  3. Dispatch role_context_plugins at hook "role.context"
  4. Assemble <bridge_context> XML from ctx["bridge_context"]
  5. Inject <bridge_context> into messages (prepend to first user message)
  6. Auto-dispatch context_stripper if client_mode != "raw"
  7. Build forward headers (endpoint auth, loopback nonce)
  8. Override model if role specifies one
  9. Forward to backend (streaming or non-streaming)

ctx contract: see notes/CONTEXT-SCHEMA.md
Plugin interface: see notes/PLUGIN-DESIGN.md
"""

import logging
import os
from typing import AsyncIterator

import httpx
from starlette.responses import StreamingResponse

from . import plugin_dispatch
from .nonce import NONCE, NONCE_HEADER

logger = logging.getLogger(__name__)

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")


def _build_ctx(raw_body: dict, headers: dict, identity_ctx=None) -> dict:
    """
    Build the pipeline ctx dict from IdentityContext + raw request data.
    See notes/CONTEXT-SCHEMA.md for the full schema.
    """
    if identity_ctx is not None:
        return {
            "identity": {
                "key": identity_ctx.identity_key,
                "name": identity_ctx.identity_name,
                "trust": identity_ctx.identity_trust,
                "token": "",  # never pass raw token downstream
                "client_mode": identity_ctx.client_mode,
            },
            "role": {
                "key": identity_ctx.role_key,
                "resource": identity_ctx.resource_key,
                "model": identity_ctx.role_model,
                "session": identity_ctx.role_session,
            },
            "resource": {
                "key": identity_ctx.resource_key,
                "endpoint_url": identity_ctx.endpoint_url,
                "endpoint_token": identity_ctx.endpoint_token,
            },
            "request": {
                "messages": list(raw_body.get("messages", [])),
                "model": raw_body.get("model", ""),
                "stream": raw_body.get("stream", False),
                "raw_body": raw_body,
            },
            "bridge_context": {},
            "headers": dict(headers),
        }
    else:
        # Zero-config mode — minimal ctx, no identity/role/resource
        return {
            "identity": {
                "key": "",
                "name": None,
                "trust": None,
                "token": "",
                "client_mode": os.getenv("CLIENT_MODE", "raw"),
            },
            "role": {"key": "", "resource": "", "model": None, "session": None},
            "resource": {
                "key": "",
                "endpoint_url": LLM_BASE_URL,
                "endpoint_token": LLM_API_KEY,
            },
            "request": {
                "messages": list(raw_body.get("messages", [])),
                "model": raw_body.get("model", ""),
                "stream": raw_body.get("stream", False),
                "raw_body": raw_body,
            },
            "bridge_context": {},
            "headers": dict(headers),
        }


def _assemble_bridge_context(ctx: dict) -> str:
    """
    Assemble <bridge_context> XML from ctx["bridge_context"].

    Keys starting with "_raw_" are injected verbatim (no wrapping tag).
    All other keys are wrapped: <key>value</key>
    """
    parts = []
    for key, value in ctx["bridge_context"].items():
        if key.startswith("_raw_"):
            parts.append(str(value))
        else:
            parts.append(f"<{key}>{value}</{key}>")

    if not parts:
        return ""
    return "<bridge_context>\n" + "\n".join(parts) + "\n</bridge_context>"


def _inject_bridge_context(ctx: dict, bridge_xml: str) -> None:
    """
    Prepend bridge_context XML to the first user message in ctx["request"]["messages"].
    If no user message exists, insert a system message.
    Modifies ctx["request"]["messages"] in place.
    """
    if not bridge_xml:
        return

    messages = ctx["request"]["messages"]

    # Find the first user message and prepend
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            messages[i] = {**msg, "content": f"{bridge_xml}\n\n{content}"}
            return

    # No user message found — insert bridge context as a system message at the start
    messages.insert(0, {"role": "system", "content": bridge_xml})


async def process(raw_body: dict, headers: dict, identity_ctx=None):
    """
    Main pipeline entry point. Returns dict (non-streaming) or StreamingResponse (streaming).
    identity_ctx: IdentityContext from auth middleware, or None in zero-config mode.
    """
    ctx = _build_ctx(raw_body, headers, identity_ctx)

    # Step 1: Identity-level context plugins (hook: "identity.context")
    if identity_ctx is not None:
        ctx = plugin_dispatch.dispatch(
            "identity.context",
            ctx,
            identity_ctx.identity_context_plugins,
        )

    # Step 2: Role-level context plugins (hook: "role.context")
    if identity_ctx is not None:
        ctx = plugin_dispatch.dispatch(
            "role.context",
            ctx,
            identity_ctx.role_context_plugins,
        )

    # Step 3: Strip client history first, THEN inject bridge_context
    # Order matters: strip the 91 LibreChat messages down to 1, then inject into that 1.
    # If we inject first, context_stripper discards the injected message along with history.
    client_mode = ctx["identity"]["client_mode"]
    if client_mode != "raw":
        ctx = plugin_dispatch.dispatch(
            "role.context",
            ctx,
            [("context_stripper", {"client_mode": client_mode})],
        )

    # Step 4: Assemble and inject <bridge_context> into the (now-stripped) messages
    bridge_xml = _assemble_bridge_context(ctx)
    _inject_bridge_context(ctx, bridge_xml)

    # Step 5: Build the outbound body
    outbound_body = dict(raw_body)
    outbound_body["messages"] = ctx["request"]["messages"]

    # Step 6: Override model if role specifies one
    role_model = ctx["role"]["model"]
    if role_model:
        outbound_body["model"] = role_model

    is_streaming = outbound_body.get("stream", False)

    if is_streaming:
        return await _forward_stream(outbound_body, headers, ctx)
    else:
        response = await _forward(outbound_body, headers, ctx)
        return response


def _build_forward_headers(headers: dict, ctx: dict) -> dict:
    """
    Pass through all headers transparently, except:
    - authorization    → replaced with backend token
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
    token = ctx["resource"]["endpoint_token"]
    forward = {
        **passthrough,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        NONCE_HEADER: NONCE,  # loopback detection
    }
    return forward


async def _forward(body: dict, headers: dict, ctx: dict) -> dict:
    """Non-streaming forward — returns parsed JSON dict."""
    forward_headers = _build_forward_headers(headers, ctx)
    url = ctx["resource"]["endpoint_url"]

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{url}/chat/completions",
            json=body,
            headers=forward_headers,
        )
        logger.info(
            f"LLM response: status={r.status_code} "
            f"content-type={r.headers.get('content-type')} "
            f"body_len={len(r.content)}"
        )
        logger.debug(f"LLM raw response body: {r.text[:2000]}")
        r.raise_for_status()
        return r.json()


async def _forward_stream(body: dict, headers: dict, ctx: dict) -> StreamingResponse:
    """Streaming forward — proxies SSE straight back to client."""
    forward_headers = _build_forward_headers(headers, ctx)
    url = ctx["resource"]["endpoint_url"]

    async def stream_generator() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{url}/chat/completions",
                json=body,
                headers=forward_headers,
            ) as r:
                logger.info(
                    f"LLM stream: status={r.status_code} "
                    f"content-type={r.headers.get('content-type')}"
                )
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
