"""
Lightweight perceptual hashing for screen region comparison.

Uses imagehash (pHash) instead of CLIP — sub-millisecond, ~1MB dependency,
no GPU or torch required. Sufficient for "is this the same VIDA screen?"
"""

import io
from PIL import Image
import imagehash


def compute_phash(image_bytes: bytes, region: tuple[int, int, int, int] | None = None) -> str:
    """
    Compute perceptual hash of an image or a cropped region.

    Args:
        image_bytes: PNG screenshot bytes
        region: Optional (x1, y1, x2, y2) crop region

    Returns:
        Hex string of the perceptual hash
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if region:
        img = img.crop(region)
    return str(imagehash.phash(img))


def compute_dhash(image_bytes: bytes, region: tuple[int, int, int, int] | None = None) -> str:
    """Compute difference hash — faster, good for exact-match detection."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if region:
        img = img.crop(region)
    return str(imagehash.dhash(img))


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    h1 = imagehash.hex_to_hash(hash1)
    h2 = imagehash.hex_to_hash(hash2)
    return h1 - h2


def hashes_match(hash1: str, hash2: str, threshold: int = 12) -> bool:
    """
    Check if two perceptual hashes are similar enough.

    Args:
        hash1, hash2: Hex hash strings
        threshold: Max Hamming distance to consider a match.
                   0 = exact match, 12 = tolerant (handles minor variations)
    """
    return hamming_distance(hash1, hash2) <= threshold
