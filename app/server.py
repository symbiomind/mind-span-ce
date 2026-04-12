"""
mind-span-ce v0.2 — plugin-native bridge server.

The core server owns exactly one route:
  GET /health — always responds 200, reports config_loaded status

All other routes are registered by server plugins during the server.startup hook.
Without a plugin that registers routes (e.g. OpenAI-Provider at server.plugins),
there are no /v1/* or any other endpoints — and that is correct.

"Not plugins bolted onto a system. A system made of plugins."
  — Sonnet, 2026-04-01
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from . import plugin_loader
from .config import get_server_cfg, is_config_loaded, load_config
from .context import StartupCtx
from .nonce import NONCE, NONCE_HEADER
from . import plugin_dispatcher
from .config import _extract_plugin_list

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────────
    logger.info(f"mind-span-ce v0.2 starting up...")
    logger.info(f"Loopback nonce: {NONCE_HEADER}={NONCE}")

    load_config()

    builtin_dir = os.path.join(os.path.dirname(__file__), "plugins", "_builtin")
    user_dir = os.path.join(os.path.dirname(__file__), "..", "plugins", "user")
    logger.info("Loading plugins...")
    plugin_loader.load_plugins(builtin_dir, user_dir)

    if is_config_loaded():
        # Fire server.startup hook — server plugins register routes here
        server_cfg = get_server_cfg()
        startup_ctx = StartupCtx(
            app=app,
            server_cfg=server_cfg,
            nonce=NONCE,
        )
        startup_plugin_list = _extract_plugin_list(server_cfg.get("plugins"))
        if startup_plugin_list:
            plugin_dispatcher.dispatch("server.startup", startup_ctx, startup_plugin_list)
        else:
            logger.info(
                "No server plugins configured. "
                "Add plugins under server.plugins in config.yml to register routes."
            )

        # Fire server.startup for context plugins declared on roles (e.g. conversational_memory).
        # These are not in server.plugins, so the loop above never reaches them — but they
        # may need to resolve resources at startup (e.g. cache MCP endpoint details).
        _fire_role_context_startup_hooks(startup_ctx)

        logger.info("Server startup hooks complete.")
    else:
        logger.info(
            "No config loaded — serving /health only. "
            "Create a config.yml to enable routing."
        )

    logger.info("mind-span-ce ready.")
    yield
    # ── Shutdown ────────────────────────────────────────────────────────────
    # Nothing to clean up in core — plugins handle their own teardown (future)


def _fire_role_context_startup_hooks(startup_ctx: "StartupCtx") -> None:
    """
    Walk all role context plugin declarations and fire server.startup for any
    plugin that supports it. Deduped by (plugin_name, resource) so the same
    resource is only initialised once even if shared across multiple roles.
    """
    from . import plugin_loader

    server_cfg = get_server_cfg()
    roles = server_cfg.get("roles", {}) or {}
    seen: set[tuple] = set()  # (plugin_name, resource_key) — dedup across roles

    for role_key, role_cfg in roles.items():
        if not role_cfg:
            continue
        context_plugins = _extract_plugin_list(
            role_cfg.get("context", {}).get("plugins")
        )
        for plugin_name, plugin_config in context_plugins:
            plugin = plugin_loader.get_plugin(plugin_name)
            if plugin is None:
                continue
            if "server.startup" not in getattr(plugin, "SUPPORTED_HOOKS", []):
                continue
            dedup_key = (plugin_name, plugin_config.get("resource", ""))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            logger.info(
                f"Firing server.startup for context plugin '{plugin_name}' "
                f"(role '{role_key}')"
            )
            try:
                plugin.hook("server.startup", startup_ctx, plugin_config)
            except Exception as e:
                logger.error(
                    f"server.startup: context plugin '{plugin_name}' raised: {e}",
                    exc_info=True,
                )


app = FastAPI(
    title="mind-span-ce",
    version="0.2.0",
    lifespan=lifespan,
    # Disable default OpenAPI docs — they'd only show /health which is misleading.
    # Plugin-registered routes won't appear here anyway (added after startup).
    docs_url=None,
    redoc_url=None,
)


@app.get("/health")
async def health():
    """
    Always responds 200. Reports whether config.yml was loaded successfully.
    Use config_loaded to confirm routing is available.
    """
    return {
        "status": "ok",
        "service": "mind-span-ce",
        "version": "0.2.0",
        "config_loaded": is_config_loaded(),
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5005)),
        reload=False,
    )
