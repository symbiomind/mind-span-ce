"""
OpenAI-Protocol — builtin plugin

Implements the OpenAI wire protocol. Add it wherever you need to speak or
understand OpenAI-format requests — on the server side, resource side, or both.

The bridge is protocol-agnostic. This plugin is one language it can speak.
A pipeline can use it on the inbound leg, outbound leg, or both independently.

Hook points:

  server.startup     — registers POST /v1/chat/completions and GET /v1/models
  resource.endpoint  — populates ctx.resource.endpoint_url + endpoint_token
  role               — model resolution, header overrides
  identity           — no-op placeholder (reserved for future per-identity setup)

Config examples:

  server:
    plugins:
      OpenAI-Protocol:
        prefix: /v1          # optional, default /v1

  resources:
    my_backend:
      endpoint:
        plugins:
          OpenAI-Protocol:
            url: http://backend:8080/v1
            token: ${MY_API_KEY}
            auth: Bearer          # optional, ignored in v0.2 (always Bearer)
            timeout: 300s         # optional, stored for future use

  roles:
    fixed_role:
      resource: my_backend
      plugins:
        OpenAI-Protocol:
          model: my/fixed-model      # client has no say — always this model
          alias: Buddy               # optional friendly name
          headers:
            x-custom-header: "value"

    flexible_role:
      resource: my_backend
      plugins:
        OpenAI-Protocol:
          models:
            default: qwen/qwen3-coder-next
            alias: qwen
            allowed:
              - qwen/qwen3-coder-next
              - [qwen, qwen/qwen3-coder-next]   # [alias, full_model_id]
              - [gpt, openai/gpt-4o]
            fetch: true    # intersect allowed list with upstream /v1/models

Model resolution rules (role hook):
  - model: present  → always use it, ignore models: entirely
  - models: only    → resolve client model via alias map, fall back to default
  - neither         → pass client's model through unchanged

GET /v1/models returns the model list scoped to the requesting identity's role.
Role plugin config takes priority over resource endpoint plugin config.

The openai Python package is available in the container image and can be used
by any plugin via `from openai import OpenAI` — no runtime pip install needed.
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

SUPPORTED_HOOKS = ["server.startup", "resource.endpoint", "role", "identity"]


# ---------------------------------------------------------------------------
# Main hook dispatcher
# ---------------------------------------------------------------------------

def hook(hook_point: str, ctx, config: dict):
    if hook_point == "server.startup":
        return _handle_server_startup(ctx, config)
    if hook_point == "resource.endpoint":
        return _handle_resource_endpoint(ctx, config)
    if hook_point == "role":
        return _handle_role(ctx, config)
    if hook_point == "identity":
        return None
    return None


# ---------------------------------------------------------------------------
# Hook 1: server.startup — register HTTP routes
# ---------------------------------------------------------------------------

def _handle_server_startup(ctx, config: dict):
    """Register POST /v1/chat/completions and GET /v1/models on the FastAPI app."""
    prefix = config.get("prefix", "/v1")

    handler = _make_handler(ctx.nonce)
    ctx.app.add_api_route(f"{prefix}/chat/completions", handler, methods=["POST"])
    logger.info(f"OpenAI-Protocol: registered route POST {prefix}/chat/completions")

    models_handler = _make_models_handler(ctx.nonce)
    ctx.app.add_api_route(f"{prefix}/models", models_handler, methods=["GET"])
    logger.info(f"OpenAI-Protocol: registered route GET {prefix}/models")

    return ctx


def _make_handler(nonce: str):
    """
    Returns a FastAPI route handler that enforces auth and calls pipeline.process().

    The nonce is captured at server.startup time and used for loopback detection.
    Imports are lazy (inside the function) to avoid circular imports at module load.
    """
    async def handle_chat_completions(request: Request):
        from app import auth
        from app import pipeline as _pipeline
        from app.nonce import NONCE_HEADER

        if request.headers.get(NONCE_HEADER) == nonce:
            return JSONResponse(
                status_code=503,
                content={"error": {
                    "message": "Loopback detected — request originated from this bridge.",
                    "type": "loopback_error",
                }},
            )

        token = auth.extract_bearer_token(request.headers.get("authorization", ""))
        if not token:
            return JSONResponse(
                status_code=401,
                content={"error": {
                    "message": "Missing or malformed Authorization header. Expected: Bearer <token>",
                    "type": "authentication_error",
                }},
            )

        identity_key = auth.resolve_identity_from_token(token)
        if identity_key is None:
            return JSONResponse(
                status_code=401,
                content={"error": {
                    "message": "Invalid token.",
                    "type": "authentication_error",
                }},
            )

        body = await request.json()
        return await _pipeline.process(body, dict(request.headers), identity_key)

    return handle_chat_completions


def _make_models_handler(nonce: str):
    """
    Returns a FastAPI route handler for GET /v1/models.

    Resolves the requesting identity's role → plugin config → model list.
    Role plugin config takes priority over resource endpoint plugin config.
    """
    async def handle_models(request: Request):
        from app import auth
        from app.nonce import NONCE_HEADER
        from app.config import resolve_identity, resolve_role, resolve_resource, _get_identity_roles

        if request.headers.get(NONCE_HEADER) == nonce:
            return JSONResponse(
                status_code=503,
                content={"error": {
                    "message": "Loopback detected.",
                    "type": "loopback_error",
                }},
            )

        token = auth.extract_bearer_token(request.headers.get("authorization", ""))
        if not token:
            return JSONResponse(
                status_code=401,
                content={"error": {
                    "message": "Missing or malformed Authorization header. Expected: Bearer <token>",
                    "type": "authentication_error",
                }},
            )

        identity_key = auth.resolve_identity_from_token(token)
        if identity_key is None:
            return JSONResponse(
                status_code=401,
                content={"error": {
                    "message": "Invalid token.",
                    "type": "authentication_error",
                }},
            )

        # Resolve identity → role → resource
        identity_cfg = resolve_identity(identity_key) or {}
        role_keys = _get_identity_roles(identity_cfg, identity_key)
        role_cfg = resolve_role(role_keys[0]) if role_keys else {}
        resource_cfg = resolve_resource((role_cfg or {}).get("resource", "")) if role_cfg else {}

        # Role plugin config takes priority over resource endpoint plugin config
        plugin_cfg = (
            (role_cfg or {}).get("plugins", {}).get("OpenAI-Protocol")
            or (resource_cfg or {}).get("endpoint", {}).get("plugins", {}).get("OpenAI-Protocol")
            or {}
        )

        models = await _build_models_list(plugin_cfg, resource_cfg)
        return JSONResponse(content={"object": "list", "data": models})

    return handle_models


# ---------------------------------------------------------------------------
# Hook 2: resource.endpoint — populate endpoint url + token
# ---------------------------------------------------------------------------

def _handle_resource_endpoint(ctx, config: dict):
    """
    Set ctx.resource.endpoint_url and ctx.resource.endpoint_token from config.

    This is the gate — without this hook running, pipeline returns 503.
    """
    url = config.get("url")
    token = config.get("token")

    if not url:
        logger.error("OpenAI-Protocol resource.endpoint: 'url' is required in config.")
        return ctx

    ctx.resource.endpoint_url = url
    ctx.resource.endpoint_token = token or ""

    timeout_raw = config.get("timeout")
    if timeout_raw is not None:
        ctx.plugin_data["OpenAI-Protocol.timeout"] = _parse_timeout(timeout_raw)

    return ctx


# ---------------------------------------------------------------------------
# Hook 3: role — model resolution + header injection
# ---------------------------------------------------------------------------

def _handle_role(ctx, config: dict):
    """
    Resolve model and inject header overrides from role config.

    Model resolution writes directly to ctx.request.model — pipeline step 12
    uses whatever is in ctx.request.model at forward time.

    Header overrides write to ctx.headers — _build_forward_headers() passes
    everything not in the hop-by-hop strip list through to the backend.
    """
    # Header injection
    for k, v in (config.get("headers") or {}).items():
        ctx.headers[k.lower()] = str(v)

    # Model resolution
    resolved = _resolve_model(ctx.request.model, config)
    if resolved is not None:
        ctx.request.model = resolved

    # Store alias for future context plugin use ({{model.alias}})
    alias = config.get("alias") or (config.get("models") or {}).get("alias")
    ctx.plugin_data["OpenAI-Protocol.alias"] = alias or ctx.request.model

    return ctx


# ---------------------------------------------------------------------------
# Model resolution helpers
# ---------------------------------------------------------------------------

def _resolve_model(client_model: str, config: dict) -> str | None:
    """
    Resolve the model to use for this request.

    Returns the resolved model string, or None to pass client_model through unchanged.

    Rules:
      model: present  → always return it (client has no say)
      models: only    → resolve via alias map, fall back to default
      neither         → return None (pass through)
    """
    fixed = config.get("model")
    if fixed:
        return fixed

    models_cfg = config.get("models")
    if not models_cfg:
        return None

    alias_map = _build_alias_map(models_cfg)

    if client_model and client_model in alias_map:
        return alias_map[client_model]

    return models_cfg.get("default") or None


def _build_alias_map(models_cfg: dict) -> dict:
    """
    Build {alias_or_id: full_model_id} from models.allowed list.

      "full/model-id"            → {full/model-id: full/model-id}
      ["alias", "full/model-id"] → {alias: full/model-id, full/model-id: full/model-id}
    """
    result = {}
    for entry in (models_cfg.get("allowed") or []):
        if isinstance(entry, str):
            result[entry] = entry
        elif isinstance(entry, list) and len(entry) == 2:
            alias, model_id = entry
            result[str(alias)] = str(model_id)
            result[str(model_id)] = str(model_id)
    return result


# ---------------------------------------------------------------------------
# GET /v1/models list builder
# ---------------------------------------------------------------------------

async def _build_models_list(plugin_cfg: dict, resource_cfg: dict) -> list[dict]:
    """Build the OpenAI-format model list for this identity's role."""
    fixed = plugin_cfg.get("model")
    alias = plugin_cfg.get("alias")

    if fixed:
        return [_model_entry(fixed, alias or fixed)]

    models_cfg = plugin_cfg.get("models")
    if not models_cfg:
        return []

    alias_map = _build_alias_map(models_cfg)

    if models_cfg.get("fetch"):
        ep = (resource_cfg or {}).get("endpoint", {}).get("plugins", {}).get("OpenAI-Protocol", {})
        upstream = await _fetch_upstream_models(ep.get("url", ""), ep.get("token", ""))
        upstream_ids = {m["id"] for m in upstream}
        seen, result = set(), []
        for alias_str, model_id in alias_map.items():
            if model_id in upstream_ids and model_id not in seen:
                seen.add(model_id)
                display = alias_str if alias_str != model_id else model_id
                result.append(_model_entry(model_id, display))
        return result

    # No fetch — return allowed list deduplicated to full model ids
    seen, result = set(), []
    for alias_str, model_id in alias_map.items():
        if model_id not in seen:
            seen.add(model_id)
            display = alias_str if alias_str != model_id else model_id
            result.append(_model_entry(model_id, display))
    return result


def _model_entry(model_id: str, display_name: str) -> dict:
    return {"id": model_id, "object": "model", "owned_by": "bridge", "display_name": display_name}


async def _fetch_upstream_models(url: str, token: str) -> list[dict]:
    """Fetch model list from upstream /v1/models. Returns empty list on failure."""
    import httpx
    if not url:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{url}/models",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return r.json().get("data", [])
    except Exception as e:
        logger.warning(f"OpenAI-Protocol: upstream /v1/models fetch failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _parse_timeout(raw) -> float:
    """Parse a timeout value. Accepts int, float, or strings like '300' or '300s'."""
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().rstrip("s")
    try:
        return float(s)
    except ValueError:
        logger.warning(f"OpenAI-Protocol: could not parse timeout '{raw}', using 300.0s")
        return 300.0
