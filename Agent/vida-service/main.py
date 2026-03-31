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

from config import API_PORT, BRIDGE_PORT, BRIDGE_SECRET
from bridge.ws_bridge import WebSocketBridge
from api import routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

bridge = WebSocketBridge(port=BRIDGE_PORT, secret=BRIDGE_SECRET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch WebSocket bridge
    await bridge.start()
    routes.bridge = bridge
    logger.info(f"VIDA agent service ready — API on {API_PORT}, bridge on {BRIDGE_PORT}")
    yield
    # Shutdown
    await bridge.stop()


app = FastAPI(title="VIDA Agent Service", lifespan=lifespan)
app.include_router(routes.router)


@app.get("/health")
async def root_health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT, log_level="info")
