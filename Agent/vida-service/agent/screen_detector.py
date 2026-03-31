"""
Detect which VIDA screen is currently displayed using perceptual hashing.

No neural network — just crop known regions and compare hashes.
Cost: <10ms per detection, zero API calls.
"""

import json
import logging
import os
from pathlib import Path

from agent.screen_hash import compute_phash, hashes_match
from agent.vida_screens import VidaScreen, SCREEN_DEFINITIONS, ScreenDefinition

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
SIGNATURES_FILE = DATA_DIR / "screen_signatures.json"

# Hamming distance threshold for screen matching
MATCH_THRESHOLD = 12


class ScreenDetector:
    """Detects which VIDA screen is visible from a screenshot."""

    def __init__(self):
        self._signatures: dict[str, dict[str, str]] = {}  # screen_name -> {region_name: hash}
        self._calibrated = False
        self._load_signatures()

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated and len(self._signatures) > 0

    def _load_signatures(self):
        """Load saved screen signatures from disk."""
        if SIGNATURES_FILE.exists():
            try:
                with open(SIGNATURES_FILE) as f:
                    self._signatures = json.load(f)
                self._calibrated = len(self._signatures) > 0
                logger.info(f"Loaded {len(self._signatures)} screen signatures")
            except Exception as e:
                logger.warning(f"Failed to load signatures: {e}")

    def _save_signatures(self):
        """Persist screen signatures to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SIGNATURES_FILE, "w") as f:
            json.dump(self._signatures, f, indent=2)
        logger.info(f"Saved {len(self._signatures)} screen signatures")

    def calibrate_screen(self, screen: VidaScreen, screenshot_bytes: bytes):
        """
        Calibrate a screen by computing hashes of its check regions.

        Call this while VIDA is showing the given screen.
        """
        definition = SCREEN_DEFINITIONS.get(screen)
        if not definition:
            raise ValueError(f"No definition for screen {screen}")

        hashes = {}
        for region in definition.check_regions:
            bbox = (region.x1, region.y1, region.x2, region.y2)
            h = compute_phash(screenshot_bytes, region=bbox)
            hashes[region.name] = h
            logger.info(f"Calibrated {screen.value}/{region.name}: {h}")

        self._signatures[screen.value] = hashes
        self._calibrated = True
        self._save_signatures()

    def detect_screen(self, screenshot_bytes: bytes) -> VidaScreen:
        """
        Identify which VIDA screen is currently displayed.

        Returns VidaScreen.UNKNOWN if no calibrated screen matches.
        """
        if not self._calibrated:
            return VidaScreen.UNKNOWN

        best_screen = VidaScreen.UNKNOWN
        best_score = float("inf")  # lower = better (sum of hamming distances)

        for screen_name, region_hashes in self._signatures.items():
            screen = VidaScreen(screen_name)
            definition = SCREEN_DEFINITIONS.get(screen)
            if not definition:
                continue

            total_distance = 0
            matched_regions = 0

            for region in definition.check_regions:
                ref_hash = region_hashes.get(region.name)
                if not ref_hash:
                    continue

                bbox = (region.x1, region.y1, region.x2, region.y2)
                current_hash = compute_phash(screenshot_bytes, region=bbox)

                if hashes_match(current_hash, ref_hash, threshold=MATCH_THRESHOLD):
                    matched_regions += 1
                    from agent.screen_hash import hamming_distance
                    total_distance += hamming_distance(current_hash, ref_hash)
                else:
                    # One region mismatch disqualifies this screen
                    total_distance = float("inf")
                    break

            # Must match ALL regions for this screen
            if matched_regions == len(definition.check_regions) and total_distance < best_score:
                best_score = total_distance
                best_screen = screen

        if best_screen != VidaScreen.UNKNOWN:
            logger.debug(f"Detected screen: {best_screen.value} (score={best_score})")
        else:
            logger.debug("Screen not recognized — returning UNKNOWN")

        return best_screen

    def get_elements(self, screen: VidaScreen) -> dict:
        """Get known UI element coordinates for a screen."""
        definition = SCREEN_DEFINITIONS.get(screen)
        if not definition:
            return {}
        return {name: {"x": el.x, "y": el.y, "description": el.description}
                for name, el in definition.elements.items()}
