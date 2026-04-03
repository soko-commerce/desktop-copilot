"""
WebSocket bridge server that replaces pig.dev.

Piglet (Zig client on Windows) connects to this server via WSS.
The bridge proxies HTTP requests to piglet over the WebSocket tunnel.

Protocol (matching tunnel.zig):
  Request:  JSON RequestMeta -> binary body chunks (16KB) -> "end" text
  Response: JSON ResponseMeta -> binary body chunks (16KB) -> "end" text
"""

import asyncio
import json
import logging
import uuid
from typing import Optional

import websockets
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger(__name__)

CHUNK_SIZE = 16 * 1024  # 16KB, matching tunnel.zig


class WebSocketBridge:
    def __init__(self, port: int, secret: str):
        self.port = port
        self.secret = secret
        self._piglet: Optional[ServerConnection] = None
        self._piglet_fingerprint: Optional[str] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._response_buffers: dict[str, dict] = {}
        self._server = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._piglet is not None

    async def _process_request(self, path, headers):
        """Fix duplicate headers from piglet/Traefik before websockets validates."""
        # Piglet sends Sec-WebSocket-Version in custom headers AND the Zig
        # websocket lib adds it automatically, causing a duplicate that
        # the Python websockets library rejects as 400.
        for key in ("Sec-WebSocket-Version", "Upgrade", "Connection"):
            values = headers.get_all(key)
            if len(values) > 1:
                del headers[key]
                headers[key] = values[0]
        return None

    async def start(self):
        self._server = await websockets.serve(
            self._handler,
            "0.0.0.0",
            self.port,
            max_size=10 * 1024 * 1024,  # 10MB max message
            ping_interval=30,
            ping_timeout=10,
            process_request=self._process_request,
        )
        logger.info(f"WebSocket bridge listening on port {self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("WebSocket bridge stopped")

    async def _handler(self, websocket):
        # Validate auth — support both legacy and new websockets API
        headers = getattr(websocket, 'request_headers', None) or getattr(websocket.request, 'headers', {})
        auth = headers.get("Authorization", "")
        if auth != f"Bearer {self.secret}":
            logger.warning("Piglet connection rejected: bad auth")
            await websocket.close(4001, "Unauthorized")
            return

        fingerprint = headers.get("X-PIGLET-FINGERPRINT", "unknown")

        if self._piglet is not None:
            logger.warning("New piglet connection replacing existing one")
            try:
                await self._piglet.close(4002, "Replaced by new connection")
            except Exception:
                pass

        self._piglet = websocket
        self._piglet_fingerprint = fingerprint
        logger.info(f"Piglet connected: fingerprint={fingerprint}")

        try:
            await self._receive_loop(websocket)
        except websockets.ConnectionClosed:
            logger.info("Piglet disconnected")
        except Exception as e:
            logger.error(f"Piglet connection error: {e}")
        finally:
            if self._piglet is websocket:
                self._piglet = None
                self._piglet_fingerprint = None
                # Fail all pending requests
                for req_id, fut in self._pending.items():
                    if not fut.done():
                        fut.set_exception(ConnectionError("Piglet disconnected"))
                self._pending.clear()
                self._response_buffers.clear()

    async def _receive_loop(self, websocket: ServerConnection):
        """Process incoming messages from piglet (response data)."""
        async for message in websocket:
            if isinstance(message, str):
                if message == "end":
                    # Find the active response being built
                    for req_id, buf in list(self._response_buffers.items()):
                        if buf.get("receiving"):
                            body = b"".join(buf["chunks"])
                            status = buf["status"]
                            headers = buf["headers"]
                            buf["receiving"] = False

                            fut = self._pending.pop(req_id, None)
                            if fut and not fut.done():
                                fut.set_result((status, headers, body))
                            del self._response_buffers[req_id]
                            break
                else:
                    # JSON response metadata
                    try:
                        meta = json.loads(message)
                        req_id = meta.get("requestId")
                        if req_id and req_id in self._response_buffers:
                            self._response_buffers[req_id]["status"] = meta.get("status", 200)
                            self._response_buffers[req_id]["headers"] = meta.get("headers", {})
                            self._response_buffers[req_id]["receiving"] = True
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON from piglet: {message[:100]}")
            elif isinstance(message, bytes):
                # Binary body chunk — append to active response
                for req_id, buf in self._response_buffers.items():
                    if buf.get("receiving"):
                        buf["chunks"].append(message)
                        break

    async def send_request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        body: bytes = b"",
        timeout: float = 60.0,
    ) -> tuple[int, dict, bytes]:
        """
        Send an HTTP request to piglet via the WebSocket tunnel.
        Returns (status_code, response_headers, response_body).
        """
        if not self._piglet:
            raise ConnectionError("No piglet connected")

        request_id = str(uuid.uuid4())

        # Prepare response buffer
        self._response_buffers[request_id] = {
            "status": 0,
            "headers": {},
            "chunks": [],
            "receiving": False,
        }

        # Create future for the response
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut

        try:
            async with self._lock:
                # Send request metadata
                meta = {
                    "requestId": request_id,
                    "method": method,
                    "path": path.lstrip("/"),
                    "headers": headers or {},
                    "query": "",
                }
                await self._piglet.send(json.dumps(meta))

                # Send body in chunks
                if body:
                    for i in range(0, len(body), CHUNK_SIZE):
                        chunk = body[i : i + CHUNK_SIZE]
                        await self._piglet.send(chunk)

                # Send end sentinel
                await self._piglet.send("end")

            # Wait for response
            return await asyncio.wait_for(fut, timeout=timeout)

        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            self._response_buffers.pop(request_id, None)
            raise TimeoutError(f"Request {method} {path} timed out after {timeout}s")
        except Exception:
            self._pending.pop(request_id, None)
            self._response_buffers.pop(request_id, None)
            raise
