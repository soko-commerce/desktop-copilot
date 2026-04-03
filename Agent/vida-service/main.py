"""
VIDA Agent Service — FastAPI + WebSocket Bridge.

Starts both:
  - FastAPI on API_PORT (default 8000) — receives search requests
  - WebSocket bridge on BRIDGE_PORT (default 8765) — piglet connects here
"""

import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from contextlib import asynccontextmanager

from config import API_PORT, BRIDGE_PORT, BRIDGE_SECRET, PIGLET_DIRECT_URL
from bridge.ws_bridge import WebSocketBridge
from bridge.direct_client import DirectPigletClient
from api import routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

if PIGLET_DIRECT_URL:
    bridge = DirectPigletClient(PIGLET_DIRECT_URL)
else:
    bridge = WebSocketBridge(port=BRIDGE_PORT, secret=BRIDGE_SECRET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bridge.start()
    routes.bridge = bridge
    if PIGLET_DIRECT_URL:
        logger.info(f"VIDA agent service ready — API on {API_PORT}, piglet direct at {PIGLET_DIRECT_URL}")
    else:
        logger.info(f"VIDA agent service ready — API on {API_PORT}, bridge on {BRIDGE_PORT}")
    yield
    await bridge.stop()


app = FastAPI(title="VIDA Agent Service", lifespan=lifespan)
app.include_router(routes.router)


@app.get("/health")
async def root_health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT, log_level="info")
