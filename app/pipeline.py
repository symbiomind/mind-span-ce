"""
Core request pipeline for mind-span-ce v0.2.

The pipeline is a library — it is NOT called directly from server.py.
Server plugins (e.g. OpenAI-Provider) register their own HTTP routes and call
pipeline.process() to handle the request through the plugin hook system.

Pipeline flow:
  1.  Build PipelineCtx from identity_key + raw request
  2.  Fire "server" hook (per-request server plugins)
  3.  Fire "resource" hook
  4.  Fire "resource.endpoint" hook → endpoint_url/token must be populated here
  5.  Fire "session" hook (if role has a session)
  6.  Fire "session.context" hook (if role has a session with context plugins)
  7.  Fire "role" hook
  8.  Fire "identity" hook
  9.  Fire "identity.context" hook (+ caller_inject sugar expansion)
  10. Fire "role.context" hook
  11. Assemble <bridge_context> XML and inject into messages
  12. Build outbound request body
  13. Build forward headers
  14. Forward to backend (streaming or non-streaming)

ctx contract: see app/context.py
Plugin interface: see notes/PLUGIN-DESIGN.md
Hook points: see docs/config.yml/README.md
"""

import logging
from typing import AsyncIterator

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from . import bridge_context as bc
from . import plugin_dispatcher
from .config import (
    _extract_plugin_list,
    get_server_cfg,
    resolve_identity,
    resolve_resource,
    resolve_role,
    resolve_session,
)
from .context import IdentityInfo, PipelineCtx, RequestInfo, ResourceInfo, RoleInfo
from .nonce import NONCE, NONCE_HEADER

logger = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "authorization", "content-length", "host",
    "transfer-encoding", "connection", "keep-alive", "cookie",
}

_FORWARD_TIMEOUT = 300.0  # seconds — configurable via OpenAI-Provider plugin in future


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def process(
    raw_body: dict,
    inbound_headers: dict,
    identity_key: str,
) -> JSONResponse | StreamingResponse:
    """
    Main pipeline entry point. Called by server plugins (not server.py directly).

    raw_body: parsed JSON body from the client request
    inbound_headers: headers from the client request (will be sanitised internally)
    identity_key: resolved identity key from config (caller's token → identity)

    Returns JSONResponse (non-streaming) or StreamingResponse (streaming).
    Returns a JSON error response on configuration failures.
    """

    # ── Step 1: Build PipelineCtx ───────────────────────────────────────────
    ctx = _build_ctx(raw_body, inbound_headers, identity_key)
    if ctx is None:
        return _config_error("Identity, role, or resource could not be resolved. Check your config.yml.")

    # ── Step 2: SERVER hook (per-request) ───────────────────────────────────
    server_cfg = get_server_cfg()
    server_plugin_list = _extract_plugin_list(server_cfg.get("plugins"))
    if server_plugin_list:
        ctx = plugin_dispatcher.dispatch("server", ctx, server_plugin_list)

    # ── Step 3: RESOURCE hook ───────────────────────────────────────────────
    resource_cfg = resolve_resource(ctx.resource.key) or {}
    resource_plugin_list = _extract_plugin_list(resource_cfg.get("plugins"))
    if resource_plugin_list:
        ctx = plugin_dispatcher.dispatch("resource", ctx, resource_plugin_list)

    # ── Step 4: RESOURCE.ENDPOINT hook ─────────────────────────────────────
    endpoint_plugin_list = _extract_plugin_list(
        resource_cfg.get("endpoint", {}).get("plugins")
    )
    if endpoint_plugin_list:
        ctx = plugin_dispatcher.dispatch("resource.endpoint", ctx, endpoint_plugin_list)

    if ctx.resource.endpoint_url is None:
        return _config_error(
            "No endpoint provider configured. "
            "Add a resource.endpoint plugin (e.g. OpenAI-Provider) to your config.yml "
            f"under resources.{ctx.resource.key}.endpoint.plugins."
        )

    # ── Step 5 & 6: SESSION hooks (conditional) ─────────────────────────────
    role_cfg = resolve_role(ctx.role.key) or {}
    session_key = ctx.role.session_key
    if session_key:
        session_cfg = resolve_session(session_key) or {}

        session_plugin_list = _extract_plugin_list(session_cfg.get("plugins"))
        if session_plugin_list:
            ctx = plugin_dispatcher.dispatch("session", ctx, session_plugin_list)

        session_context_plugin_list = _extract_plugin_list(
            session_cfg.get("context", {}).get("plugins")
        )
        if session_context_plugin_list:
            ctx = plugin_dispatcher.dispatch("session.context", ctx, session_context_plugin_list)

    # ── Step 7: ROLE hook ───────────────────────────────────────────────────
    role_plugin_list = _extract_plugin_list(role_cfg.get("plugins"))
    if role_plugin_list:
        ctx = plugin_dispatcher.dispatch("role", ctx, role_plugin_list)

    # ── Step 8: IDENTITY hook ───────────────────────────────────────────────
    identity_cfg = resolve_identity(identity_key) or {}
    identity_plugin_list = _extract_plugin_list(identity_cfg.get("plugins"))
    if identity_plugin_list:
        ctx = plugin_dispatcher.dispatch("identity", ctx, identity_plugin_list)

    # ── Step 9: IDENTITY.CONTEXT hook ──────────────────────────────────────
    identity_context_cfg = identity_cfg.get("context", {}) or {}
    identity_context_plugins = _extract_plugin_list(identity_context_cfg.get("plugins"))

    # Sugar expansion: context.name/trust → prepend caller_inject if not already declared
    identity_name = identity_context_cfg.get("name")
    if identity_name:
        first_plugin = identity_context_plugins[0][0] if identity_context_plugins else None
        if first_plugin != "caller_inject":
            caller_cfg = {"name": identity_name}
            if identity_context_cfg.get("trust"):
                caller_cfg["trust"] = identity_context_cfg["trust"]
            identity_context_plugins = [("caller_inject", caller_cfg)] + identity_context_plugins

    if identity_context_plugins:
        ctx = plugin_dispatcher.dispatch("identity.context", ctx, identity_context_plugins)

    # ── Step 10: ROLE.CONTEXT hook ──────────────────────────────────────────
    role_context_plugins = _extract_plugin_list(
        role_cfg.get("context", {}).get("plugins")
    )
    if role_context_plugins:
        ctx = plugin_dispatcher.dispatch("role.context", ctx, role_context_plugins)

    # ── Step 11: Assemble and inject <bridge_context> ───────────────────────
    bridge_xml = bc.assemble(ctx.bridge_context)
    ctx.request.messages = bc.inject_into_messages(ctx.request.messages, bridge_xml)

    # ── Step 12: Build outbound body ────────────────────────────────────────
    outbound_body = dict(raw_body)
    outbound_body["messages"] = ctx.request.messages
    outbound_body["model"] = ctx.request.model  # plugins (e.g. OpenAI-Protocol role hook) may have updated this

    # ── Step 13: Build forward headers ──────────────────────────────────────
    forward_headers = _build_forward_headers(inbound_headers, ctx)

    # ── Step 14: Forward to backend ─────────────────────────────────────────
    is_streaming = outbound_body.get("stream", False)
    endpoint_url = ctx.resource.endpoint_url

    if is_streaming:
        return await _forward_stream(endpoint_url, outbound_body, forward_headers)
    else:
        return await _forward(endpoint_url, outbound_body, forward_headers)


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------

def _build_ctx(
    raw_body: dict,
    inbound_headers: dict,
    identity_key: str,
) -> PipelineCtx | None:
    """
    Build the PipelineCtx from config lookups and the raw request.
    Returns None if required config blocks cannot be resolved.
    """
    identity_cfg = resolve_identity(identity_key)
    if not identity_cfg:
        logger.error(f"Could not resolve identity config for key '{identity_key}'.")
        return None

    # Resolve first role (multi-role fan-out is v0.2+ — first role is used)
    from .config import _get_identity_roles
    role_keys = _get_identity_roles(identity_cfg, identity_key)
    if not role_keys:
        logger.error(f"Identity '{identity_key}' has no roles.")
        return None
    role_key = role_keys[0]

    role_cfg = resolve_role(role_key)
    if not role_cfg:
        logger.error(f"Could not resolve role config for key '{role_key}'.")
        return None

    resource_key = role_cfg.get("resource")
    if not resource_key:
        logger.error(f"Role '{role_key}' has no resource.")
        return None

    resource_cfg = resolve_resource(resource_key)
    if not resource_cfg:
        logger.error(f"Could not resolve resource config for key '{resource_key}'.")
        return None

    # Extract identity context sugar fields
    identity_context = identity_cfg.get("context", {}) or {}

    messages = list(raw_body.get("messages", []))

    return PipelineCtx(
        identity=IdentityInfo(
            key=identity_key,
            name=identity_context.get("name"),
            trust=identity_context.get("trust"),
            client_mode=identity_cfg.get("client_mode", "raw"),
        ),
        role=RoleInfo(
            key=role_key,
            resource_key=resource_key,
            session_key=role_cfg.get("session"),
        ),
        resource=ResourceInfo(
            key=resource_key,
            endpoint_url=None,    # populated by resource.endpoint plugin
            endpoint_token=None,  # populated by resource.endpoint plugin
        ),
        request=RequestInfo(
            original_messages=list(messages),  # immutable reference copy
            messages=messages,                 # working copy
            model=raw_body.get("model", ""),
            stream=raw_body.get("stream", False),
            raw_body=raw_body,
        ),
        headers=_sanitise_headers(inbound_headers),
    )


def _sanitise_headers(headers: dict) -> dict:
    """Lowercase all header keys and strip hop-by-hop headers."""
    return {
        k.lower(): v
        for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


# ---------------------------------------------------------------------------
# Forward helpers
# ---------------------------------------------------------------------------

def _build_forward_headers(inbound_headers: dict, ctx: PipelineCtx) -> dict:
    """
    Build the headers to send to the backend.

    Pass through all client headers, except:
    - authorization    → replaced with backend endpoint token
    - content-length   → recalculated by httpx (body may have changed)
    - host             → must be upstream host, not client's
    - transfer-encoding, connection, keep-alive → hop-by-hop, breaks proxies
    - cookie           → security: don't leak client session cookies upstream
    """
    # ctx.headers is the sanitised, plugin-augmented header set.
    # It starts as a copy of inbound_headers (hop-by-hop stripped, keys lowercased)
    # and may have been extended by plugins (e.g. OpenAI-Protocol role hook injects
    # x-openclaw-* headers). Use it as the source of truth for passthrough.
    passthrough = {
        k: v for k, v in ctx.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return {
        **passthrough,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ctx.resource.endpoint_token}",
        NONCE_HEADER: NONCE,
    }


async def _forward(url: str, body: dict, headers: dict) -> JSONResponse:
    """Non-streaming forward — returns JSONResponse."""
    async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT) as client:
        r = await client.post(
            f"{url}/chat/completions",
            json=body,
            headers=headers,
        )
        logger.info(
            f"Backend response: status={r.status_code} "
            f"content-type={r.headers.get('content-type')} "
            f"body_len={len(r.content)}"
        )
        logger.debug(f"Backend raw response: {r.text[:2000]}")
        r.raise_for_status()
        return JSONResponse(content=r.json(), status_code=r.status_code)


async def _forward_stream(url: str, body: dict, headers: dict) -> StreamingResponse:
    """Streaming forward — proxies SSE bytes straight back to the client."""
    async def stream_generator() -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{url}/chat/completions",
                json=body,
                headers=headers,
            ) as r:
                logger.info(
                    f"Backend stream: status={r.status_code} "
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


# ---------------------------------------------------------------------------
# Error response helpers
# ---------------------------------------------------------------------------

def _config_error(message: str) -> JSONResponse:
    """Return an OpenAI-envelope error response for configuration failures."""
    logger.error(f"Pipeline configuration error: {message}")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": message, "type": "configuration_error"}},
    )
