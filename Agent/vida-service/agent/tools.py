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
from agent.calibration import get_calibrator

logger = logging.getLogger(__name__)

# Actual screen dimensions (gdigrab) — set after first dimensions() call
_screen_w: Optional[int] = None
_screen_h: Optional[int] = None


def _to_screen(model_x: int, model_y: int) -> tuple[int, int]:
    calibrator = get_calibrator()
    if calibrator.is_calibrated:
        return calibrator.to_screen(model_x, model_y)
    # Fallback: use gdigrab dimensions directly (pre-calibration behavior)
    if _screen_w is None or _screen_h is None:
        return model_x, model_y
    x = int(model_x * _screen_w / MODEL_WIDTH)
    y = int(model_y * _screen_h / MODEL_HEIGHT)
    return max(0, min(x, _screen_w - 1)), max(0, min(y, _screen_h - 1))


def _to_model(screen_x: int, screen_y: int) -> tuple[int, int]:
    calibrator = get_calibrator()
    if calibrator.is_calibrated:
        return calibrator.to_model(screen_x, screen_y)
    # Fallback: use gdigrab dimensions directly
    if _screen_w is None or _screen_h is None:
        return screen_x, screen_y
    x = int(screen_x * MODEL_WIDTH / _screen_w)
    y = int(screen_y * MODEL_HEIGHT / _screen_h)
    return max(0, min(x, MODEL_WIDTH - 1)), max(0, min(y, MODEL_HEIGHT - 1))


async def get_dimensions(bridge: WebSocketBridge) -> str:
    """Get screen dimensions, cache them, and auto-calibrate if needed."""
    global _screen_w, _screen_h
    status, _, body = await bridge.send_request("GET", "computer/display/dimensions")
    if status != 200:
        return f"Error: status {status}"
    data = json.loads(body)
    _screen_w = data.get("width", MODEL_WIDTH)
    _screen_h = data.get("height", MODEL_HEIGHT)

    # Auto-calibrate on first call or when gdigrab dimensions change
    calibrator = get_calibrator()
    if calibrator.needs_calibration(_screen_w, _screen_h):
        fingerprint = getattr(bridge, '_piglet_fingerprint', '') or ''
        try:
            await calibrator.calibrate(bridge, _screen_w, _screen_h, fingerprint)
        except Exception as e:
            logger.error(f"Auto-calibration failed: {e}", exc_info=True)

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


async def _click(bridge: WebSocketBridge, button: str, sx: int, sy: int) -> str:
    """Send a full click (press + release) at screen coordinates."""
    down = json.dumps({"button": button, "x": sx, "y": sy, "down": True}).encode()
    up = json.dumps({"button": button, "x": sx, "y": sy, "down": False}).encode()
    await bridge.send_request("POST", "computer/input/mouse/click", body=down)
    status, _, _ = await bridge.send_request("POST", "computer/input/mouse/click", body=up)
    return "ok" if status == 200 else f"Error: status {status}"


async def left_click(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Left-click at model coordinates."""
    sx, sy = _to_screen(x, y)
    return await _click(bridge, "left", sx, sy)


async def right_click(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Right-click at model coordinates."""
    sx, sy = _to_screen(x, y)
    return await _click(bridge, "right", sx, sy)


async def double_click(bridge: WebSocketBridge, x: int, y: int) -> str:
    """Double-click at model coordinates."""
    sx, sy = _to_screen(x, y)
    await _click(bridge, "left", sx, sy)
    return await _click(bridge, "left", sx, sy)


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


async def screenshot_region(bridge: WebSocketBridge, x: int, y: int, w: int, h: int) -> str:
    """Take a screenshot of a specific screen region. Returns base64 PNG.

    Coordinates are in native display space (not model space).
    Much smaller payload than full screenshot — useful for screen detection.
    """
    path = f"computer/display/screenshot?x={x}&y={y}&w={w}&h={h}"
    status, _, body = await bridge.send_request("GET", path, timeout=30.0)
    if status != 200:
        return f"Error taking region screenshot: status {status}"
    return base64.b64encode(body).decode()


async def powershell_exec(bridge: WebSocketBridge, command: str) -> dict:
    """Execute a PowerShell command on the remote machine.

    Returns: {"stdout": str, "stderr": str, "exitCode": int}
    """
    payload = json.dumps({"command": command}).encode()
    status, _, body = await bridge.send_request(
        "POST", "computer/shell/powershell/exec", body=payload, timeout=60.0
    )
    if status != 200:
        return {"stdout": "", "stderr": f"Error: status {status}", "exitCode": -1}
    return json.loads(body)
