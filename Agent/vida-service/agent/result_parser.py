"""
Parse structured parts data from the agent's final response.

The agent navigates VIDA and describes what it sees. This module
extracts structured part information from that natural language output.
"""

import json
import logging
import re
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class VidaPart:
    part_number: str
    description: str = ""
    quantity: int = 0
    notes: str = ""


def parse_agent_response(text: str) -> list[dict]:
    """
    Extract part data from the agent's final text response.

    Tries JSON first (if the agent outputs structured data),
    then falls back to regex extraction of part-number-like patterns.
    """
    # Try JSON block
    json_match = re.search(r"```json\s*([\s\S]*?)```", text)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "parts" in data:
                return data["parts"]
        except json.JSONDecodeError:
            pass

    # Try inline JSON array
    array_match = re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group(0))
        except json.JSONDecodeError:
            pass

    # Fallback: extract part-number-like patterns (alphanumeric, 5-15 chars)
    parts = []
    seen = set()
    for match in re.finditer(r"\b([A-Z0-9]{5,15})\b", text):
        pn = match.group(1)
        if pn not in seen and not pn.isalpha():  # Must have at least one digit
            seen.add(pn)
            parts.append({"partNumber": pn, "description": "", "found": True})

    return parts
