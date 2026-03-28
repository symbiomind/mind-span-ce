"""
mind-span-ce — OpenAI-compatible proxy server.

Exposes: POST /v1/chat/completions
Startup: loads all plugins, then starts FastAPI via uvicorn.
"""

import logging
import os

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import plugin_loader
from .nonce import NONCE, NONCE_HEADER
from .pipeline import process

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="mind-span-ce", version="0.1.0")


@app.on_event("startup")
async def startup():
    logger.info(f"Loopback nonce: {NONCE_HEADER}={NONCE}")
    logger.info("Loading plugins...")
    plugin_loader.load_all()
    logger.info("mind-span-ce ready.")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Loopback detection — if our own nonce arrives, we're calling ourselves
    if request.headers.get(NONCE_HEADER) == NONCE:
        logger.error("Loopback detected! LLM_BASE_URL is pointing back at mind-span-ce. Check your .env.")
        raise HTTPException(status_code=503, detail="Mind-span loopback detected. Check LLM_BASE_URL in your .env.")

    body = await request.json()
    headers = dict(request.headers)
    response = await process(body, headers)
    # process() returns either a StreamingResponse or a dict
    if isinstance(response, dict):
        return JSONResponse(content=response)
    return response  # StreamingResponse passes through directly


@app.get("/health")
async def health():
    return {"status": "ok", "service": "mind-span-ce"}


if __name__ == "__main__":
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5005)),
        reload=False,
    )
