"""
Pipeline context objects for mind-span-ce v0.2.

PipelineCtx — shared mutable state passed through every plugin hook during a request.
StartupCtx  — passed to server plugins during the server.startup hook.

Plugin interface:
  plugin.hook(hook_point: str, ctx: PipelineCtx | StartupCtx, config: dict) -> PipelineCtx | None

See docs/config.yml/README.md for the config shape and hook points.
See notes/PLUGIN-DESIGN.md for the plugin authoring contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


# ---------------------------------------------------------------------------
# PipelineCtx sub-objects
# ---------------------------------------------------------------------------

@dataclass
class RequestInfo:
    """The inbound request — both original and the working copy plugins modify."""
    original_messages: list[dict]
    # Raw messages from the client. NEVER mutated after construction.
    # session / memory plugins use this to see what the user actually said,
    # regardless of what other plugins have done to the working copy.

    messages: list[dict]
    # Working copy. Plugins mutate this freely. This is what gets forwarded.

    model: str
    # Model string from the client request body.

    stream: bool
    # Whether the client requested a streaming response.

    raw_body: dict
    # Full original request body — read-only reference.
    # Use this to pass fields to the backend that the pipeline doesn't inspect.


@dataclass
class IdentityInfo:
    """Resolved identity from config — who is making this request."""
    key: str
    # Config key e.g. "Martin_crabby"

    name: str | None
    # From context.name sugar — becomes <caller>name</caller> via caller_inject plugin.

    trust: str | None
    # From context.trust sugar — e.g. "trusted", "public", "operator".

    client_mode: str
    # "raw" | "librechat" — used by context_stripper plugin to know how to strip.


@dataclass
class RoleInfo:
    """Resolved role — what capabilities and resources this request uses."""
    key: str
    # Config key e.g. "crabbySession"

    resource_key: str
    # The resource this role routes to.

    session_key: str | None
    # The session this role uses, or None if the backend owns the session.


@dataclass
class ResourceInfo:
    """Resolved resource — where to forward the request."""
    key: str
    # Config key e.g. "openclaw"

    endpoint_url: str | None = None
    # Populated by the resource.endpoint plugin (e.g. OpenAI-Provider).
    # If still None after resource.endpoint hook fires, pipeline returns 503.

    endpoint_token: str | None = None
    # Bearer token for the backend endpoint.
    # Populated by the resource.endpoint plugin.


# ---------------------------------------------------------------------------
# PipelineCtx — the main per-request context object
# ---------------------------------------------------------------------------

@dataclass
class PipelineCtx:
    """
    Shared mutable state for a single request pipeline pass.

    Passed to every plugin hook during request processing. Plugins receive this,
    optionally modify it, and return it (or return None to pass through unchanged).

    Hook point reference (config path → hook_point string):
      server.plugins                → "server"          (per-request)
      resources.*.plugins           → "resource"
      resources.*.endpoint.plugins  → "resource.endpoint"
      sessions.*.plugins            → "session"
      sessions.*.context.plugins    → "session.context"
      roles.*.plugins               → "role"
      identities.*.plugins          → "identity"
      identities.*.context.plugins  → "identity.context"
      roles.*.context.plugins       → "role.context"
    """

    # ── Resolved identity chain (set before pipeline starts) ────────────────
    identity: IdentityInfo
    role: RoleInfo
    resource: ResourceInfo

    # ── The request ──────────────────────────────────────────────────────────
    request: RequestInfo

    # ── Plugin output surfaces ───────────────────────────────────────────────
    bridge_context: dict[str, Any] = field(default_factory=dict)
    """
    Context contributions that will be assembled into <bridge_context> XML and
    injected into the user message before forwarding to the backend.

    Keys become XML tags:
        ctx.bridge_context["current_time"] = "Wednesday, 1st April 2026"
        → <current_time>Wednesday, 1st April 2026</current_time>

    Keys starting with "_raw_" are injected verbatim (no wrapping tag):
        ctx.bridge_context["_raw_caller"] = '<caller trust="trusted">Martin</caller>'
        → <caller trust="trusted">Martin</caller>

    Order is insertion order (Python 3.7+ dict). Plugins that run later can
    overwrite earlier entries if they use the same key.
    """

    plugin_data: dict[str, Any] = field(default_factory=dict)
    """
    Free-form plugin-to-plugin communication namespace.
    Core never reads this — it exists purely for plugins to coordinate.

    Convention: namespace your keys to avoid collisions.
        ctx.plugin_data["memory_recall.results"] = [...]
        ctx.plugin_data["session_manager.history"] = [...]
        ctx.plugin_data["quantum_universe.black_hole_radius"] = 42

    No enforcement — plugins own their namespace by convention.
    """

    # ── Inbound headers ──────────────────────────────────────────────────────
    headers: dict[str, str] = field(default_factory=dict)
    """
    Sanitised inbound headers from the client.
    Keys are lowercased. Hop-by-hop headers are stripped at intake.
    Pass-through headers (x-openclaw-*, etc.) are preserved here.
    """


# ---------------------------------------------------------------------------
# StartupCtx — passed to server plugins during server.startup hook
# ---------------------------------------------------------------------------

@dataclass
class StartupCtx:
    """
    Context for the server.startup hook — fired once during lifespan startup.

    Server plugins receive this to register FastAPI routes, open connections,
    warm caches, or perform any one-time initialization.

    Example — a plugin registering a route:
        def hook(hook_point, ctx, config):
            if hook_point == "server.startup":
                ctx.app.add_api_route(
                    "/v1/chat/completions",
                    my_handler,
                    methods=["POST"],
                )
                return ctx
    """

    app: "FastAPI"
    # The FastAPI application instance. Plugins call app.add_api_route() here
    # to register their own endpoints. The bridge core owns /health only.

    server_cfg: dict
    # Full server: config block (env vars already expanded).
    # Plugins can read their own config from server_cfg["plugins"][plugin_name].

    nonce: str
    # The loopback detection nonce for this process.
    # Server plugins that forward requests should inject this as x-mind-span-nonce.
