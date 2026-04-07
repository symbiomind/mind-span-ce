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
        startup_plugin_list = _extract_plugin_list(server_cfg.get("plugins"))
        if startup_plugin_list:
            startup_ctx = StartupCtx(
                app=app,
                server_cfg=server_cfg,
                nonce=NONCE,
            )
            plugin_dispatcher.dispatch("server.startup", startup_ctx, startup_plugin_list)
            logger.info("Server startup hooks complete.")
        else:
            logger.info(
                "No server plugins configured. "
                "Add plugins under server.plugins in config.yml to register routes."
            )
    else:
        logger.info(
            "No config loaded — serving /health only. "
            "Create a config.yml to enable routing."
        )

    logger.info("mind-span-ce ready.")
    yield
    # ── Shutdown ────────────────────────────────────────────────────────────
    # Nothing to clean up in core — plugins handle their own teardown (future)


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
