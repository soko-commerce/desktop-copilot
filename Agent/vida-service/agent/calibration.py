"""
Coordinate auto-calibration via Windows system metrics.

Problem: gdigrab captures at physical pixel resolution (e.g., 3840x2160),
but SendInput absolute mode maps through the primary monitor's logical
dimensions (e.g., 1920x1080 at 200% DPI). This module queries the actual
SendInput target space and computes correct scale factors.

Uses piglet's GET /computer/display/metrics endpoint which directly calls
GetSystemMetrics — no shell, no PowerShell, no privileges needed.

Usage:
    from agent.calibration import get_calibrator
    cal = get_calibrator()
    x, y = cal.to_screen(model_x, model_y)
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import MODEL_WIDTH, MODEL_HEIGHT

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "data" / "calibration.json"


@dataclass
class CalibrationResult:
    gdigrab_w: int          # What gdigrab reports (e.g., 3840)
    gdigrab_h: int
    input_w: int            # SM_CXSCREEN — actual SendInput space (e.g., 1920)
    input_h: int
    virtual_w: int          # SM_CXVIRTUALSCREEN
    virtual_h: int
    dpi_scale: float        # gdigrab_w / input_w (effective DPI ratio)
    scale_x: float          # input_w / MODEL_WIDTH
    scale_y: float          # input_h / MODEL_HEIGHT
    offset_x: float = 0.0
    offset_y: float = 0.0
    calibrated_at: str = ""
    fingerprint: str = ""


class CoordinateCalibrator:
    """Singleton coordinate calibrator with disk-backed cache."""

    def __init__(self):
        self._result: Optional[CalibrationResult] = None
        self._cache: dict = {}
        self._load_cache()

    def to_screen(self, model_x: int, model_y: int) -> tuple[int, int]:
        """Convert model coordinates to screen coordinates using calibration."""
        if self._result is None:
            return model_x, model_y
        r = self._result
        x = int(model_x * r.scale_x + r.offset_x)
        y = int(model_y * r.scale_y + r.offset_y)
        return max(0, min(x, r.input_w - 1)), max(0, min(y, r.input_h - 1))

    def to_model(self, screen_x: int, screen_y: int) -> tuple[int, int]:
        """Convert screen coordinates back to model coordinates."""
        if self._result is None:
            return screen_x, screen_y
        r = self._result
        x = int((screen_x - r.offset_x) / r.scale_x) if r.scale_x else 0
        y = int((screen_y - r.offset_y) / r.scale_y) if r.scale_y else 0
        return max(0, min(x, MODEL_WIDTH - 1)), max(0, min(y, MODEL_HEIGHT - 1))

    @property
    def is_calibrated(self) -> bool:
        return self._result is not None

    @property
    def result(self) -> Optional[CalibrationResult]:
        return self._result

    def needs_calibration(self, gdigrab_w: int, gdigrab_h: int) -> bool:
        """True if no result or gdigrab dimensions changed."""
        if self._result is None:
            return True
        return (self._result.gdigrab_w != gdigrab_w or
                self._result.gdigrab_h != gdigrab_h)

    async def calibrate(self, bridge, gdigrab_w: int, gdigrab_h: int,
                        fingerprint: str = "") -> CalibrationResult:
        """Query piglet's display metrics endpoint and compute calibration.

        Args:
            bridge: WebSocketBridge or DirectPigletClient
            gdigrab_w: gdigrab-reported width (physical pixels)
            gdigrab_h: gdigrab-reported height (physical pixels)
            fingerprint: optional piglet fingerprint
        """
        cache_key = f"{gdigrab_w}x{gdigrab_h}"

        # Check disk cache first
        if cache_key in self._cache:
            logger.info(f"Coordinate calibration loaded from cache for {cache_key}")
            self._result = CalibrationResult(**self._cache[cache_key])
            return self._result

        # Query piglet's display metrics endpoint (direct Win32 API, no shell needed)
        logger.info("Running coordinate calibration via piglet display metrics...")
        status, _, body = await bridge.send_request(
            "GET", "computer/display/metrics", timeout=10.0
        )

        if status != 200:
            logger.warning(
                f"Display metrics endpoint returned {status} "
                f"(piglet may need rebuild — falling back to gdigrab dims)"
            )
            return self._fallback(gdigrab_w, gdigrab_h, fingerprint)

        try:
            metrics = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse display metrics: {e}")
            return self._fallback(gdigrab_w, gdigrab_h, fingerprint)

        input_w = metrics["pw"]     # SM_CXSCREEN
        input_h = metrics["ph"]     # SM_CYSCREEN
        virtual_w = metrics.get("vw", input_w)
        virtual_h = metrics.get("vh", input_h)

        # DPI scale = physical / logical (e.g., 3840 / 1920 = 2.0)
        dpi_scale = gdigrab_w / input_w if input_w else 1.0

        scale_x = input_w / MODEL_WIDTH
        scale_y = input_h / MODEL_HEIGHT

        self._result = CalibrationResult(
            gdigrab_w=gdigrab_w,
            gdigrab_h=gdigrab_h,
            input_w=input_w,
            input_h=input_h,
            virtual_w=virtual_w,
            virtual_h=virtual_h,
            dpi_scale=dpi_scale,
            scale_x=scale_x,
            scale_y=scale_y,
            calibrated_at=datetime.now(timezone.utc).isoformat(),
            fingerprint=fingerprint,
        )

        logger.info(
            f"Calibration complete: gdigrab={gdigrab_w}x{gdigrab_h}, "
            f"input={input_w}x{input_h}, DPI scale={dpi_scale:.1f}x, "
            f"scale=({scale_x:.4f}, {scale_y:.4f})"
        )

        # Persist to disk cache
        self._cache[cache_key] = asdict(self._result)
        self._save_cache()

        return self._result

    def _fallback(self, gdigrab_w: int, gdigrab_h: int,
                  fingerprint: str) -> CalibrationResult:
        """Use gdigrab dimensions as-is when metrics endpoint unavailable."""
        logger.warning(
            f"Using fallback calibration (gdigrab dims): {gdigrab_w}x{gdigrab_h}"
        )
        self._result = CalibrationResult(
            gdigrab_w=gdigrab_w,
            gdigrab_h=gdigrab_h,
            input_w=gdigrab_w,
            input_h=gdigrab_h,
            virtual_w=gdigrab_w,
            virtual_h=gdigrab_h,
            dpi_scale=1.0,
            scale_x=gdigrab_w / MODEL_WIDTH,
            scale_y=gdigrab_h / MODEL_HEIGHT,
            calibrated_at=datetime.now(timezone.utc).isoformat(),
            fingerprint=fingerprint,
        )
        return self._result

    def invalidate(self):
        """Force re-calibration on next check."""
        self._result = None

    def _load_cache(self):
        """Load calibration cache from disk."""
        try:
            if CACHE_FILE.exists():
                self._cache = json.loads(CACHE_FILE.read_text())
                logger.debug(f"Loaded calibration cache: {list(self._cache.keys())}")
        except Exception as e:
            logger.warning(f"Failed to load calibration cache: {e}")
            self._cache = {}

    def _save_cache(self):
        """Persist calibration cache to disk."""
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save calibration cache: {e}")


# Module-level singleton
_calibrator: Optional[CoordinateCalibrator] = None


def get_calibrator() -> CoordinateCalibrator:
    """Get or create the singleton calibrator."""
    global _calibrator
    if _calibrator is None:
        _calibrator = CoordinateCalibrator()
    return _calibrator
