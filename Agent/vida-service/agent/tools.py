"""
Computer-use tools that proxy through the WebSocket bridge to piglet.

All tools match the REST endpoints that piglet's server.zig exposes.
Coordinate conversion: model uses 1024x768, actual screen may differ.
"""

import base64
import json
import logging
from typing import Optional

from bridge.ws_bridge import WebSocketBridge
from config import MODEL_WIDTH, MODEL_HEIGHT

logger = logging.getLogger(__name__)

# Actual screen dimensions — set after first dimensions() call
_screen_w: Optional[int] = None
_screen_h: Optional[int] = None


def _to_screen(model_x: int, model_y: int) -> tuple[int, int]:
    if _screen_w is None or _screen_h is None:
        return model_x, model_y
    x = int(model_x * _screen_w / MODEL_WIDTH)
    y = int(model_y * _screen_h / MODEL_HEIGHT)
    return max(0, min(x, _screen_w - 1)), max(0, min(y, _screen_h - 1))


def _to_model(screen_x: int, screen_y: int) -> tuple[int, int]:
    if _screen_w is None or _screen_h is None:
        return screen_x, screen_y
    x = int(screen_x * MODEL_WIDTH / _screen_w)
    y = int(screen_y * MODEL_HEIGHT / _screen_h)
    return max(0, min(x, MODEL_WIDTH - 1)), max(0, min(y, MODEL_HEIGHT - 1))


async def get_dimensions(bridge: WebSocketBridge) -> str:
    """Get screen dimensions and cache them."""
    global _screen_w, _screen_h
    status, _, body = await bridge.send_request("GET", "computer/display/dimensions")
    if status != 200:
        return f"Error: status {status}"
    data = json.loads(body)
    _screen_w = data.get("width", MODEL_WIDTH)
    _screen_h = data.get("height", MODEL_HEIGHT)
    return f"Screen dimensions: {_screen_w}x{_screen_h}"


async def screenshot(bridge: WebSocketBridge) -> str:
    """Take screenshot, return base64 PNG."""
    status, headers, body = await bridge.send_request(
        "GET", "computer/display/screenshot", timeout=30.0
    )
    if status != 200:
        return f"Error taking screenshot: status {status}"
    return base64.b64encode(body).decode()


async def type_text(bridge: WebSocketBridge, text: str) -> str:
    """Type text at current cursor position."""
    payload = json.dumps({"text": text}).encode()
    status, _, _ = await bridge.send_request(
        "POST", "computer/input/keyboard/type", body=payload
    )
    return "ok" if status == 200 else f"Error: status {status}"


async def key_press(bridge: WebSocketBridge, combo: str) -> str:
    """Press key combo in XDO format: 'Return', 'ctrl+c', 'Tab'."""
    payload = json.dumps({"text": combo}).encode()
    status, _, _ = await bridge.send_request(
        "POST", "computer/input/keyboard/key", body=payload
    )
    return "ok" if status == 200 else f"Error: status {status}"


async def left_click(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Left-click at model coordinates."""
    sx, sy = _to_screen(x, y)
    payload = json.dumps({"button": "left", "x": sx, "y": sy}).encode()
    status, _, _ = await bridge.send_request(
        "POST", "computer/input/mouse/click", body=payload
    )
    return "ok" if status == 200 else f"Error: status {status}"


async def right_click(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Right-click at model coordinates."""
    sx, sy = _to_screen(x, y)
    payload = json.dumps({"button": "right", "x": sx, "y": sy}).encode()
    status, _, _ = await bridge.send_request(
        "POST", "computer/input/mouse/click", body=payload
    )
    return "ok" if status == 200 else f"Error: status {status}"


async def double_click(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Double-click at model coordinates."""
    sx, sy = _to_screen(x, y)
    # First click
    payload = json.dumps({"button": "left", "x": sx, "y": sy}).encode()
    await bridge.send_request("POST", "computer/input/mouse/click", body=payload)
    # Second click
    status, _, _ = await bridge.send_request(
        "POST", "computer/input/mouse/click", body=payload
    )
    return "ok" if status == 200 else f"Error: status {status}"


async def mouse_move(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Move mouse to model coordinates."""
    sx, sy = _to_screen(x, y)
    payload = json.dumps({"x": sx, "y": sy}).encode()
    status, _, _ = await bridge.send_request(
        "POST", "computer/input/mouse/move", body=payload
    )
    return "ok" if status == 200 else f"Error: status {status}"


async def cursor_position(bridge: WebSocketBridge) -> str:
    """Get current cursor position in model coordinates."""
    status, _, body = await bridge.send_request("GET", "computer/input/mouse/position")
    if status != 200:
        return f"Error: status {status}"
    data = json.loads(body)
    mx, my = _to_model(data["x"], data["y"])
    return f"Cursor at ({mx}, {my})"
