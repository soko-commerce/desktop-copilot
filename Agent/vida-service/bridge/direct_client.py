"""
Direct HTTP client for piglet — bypasses WebSocket tunnel.

Use when piglet runs locally (piglet start --port 3000) on the same machine.
Set PIGLET_DIRECT_URL=http://localhost:3000 to enable.

Implements the same send_request() interface as WebSocketBridge so all
agent/tools code works unchanged.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class DirectPigletClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=60.0)
        self._piglet_fingerprint: Optional[str] = "direct-local"

    @property
    def connected(self) -> bool:
        """Assume connected — health check will confirm."""
        return True

    async def start(self):
        logger.info(f"Direct piglet client targeting {self.base_url}")

    async def stop(self):
        await self._client.aclose()

    async def send_request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        body: bytes = b"",
        timeout: float = 60.0,
    ) -> tuple[int, dict, bytes]:
        """
        Send HTTP request directly to piglet's local server.
        Same signature as WebSocketBridge.send_request().
        """
        url = f"/{path.lstrip('/')}"
        try:
            response = await self._client.request(
                method=method,
                url=url,
                headers=headers,
                content=body if body else None,
                timeout=timeout,
            )
            return (
                response.status_code,
                dict(response.headers),
                response.content,
            )
        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot reach piglet at {self.base_url} — is 'piglet start' running?"
            )
        except httpx.TimeoutException:
            raise TimeoutError(f"Request {method} {url} timed out after {timeout}s")
