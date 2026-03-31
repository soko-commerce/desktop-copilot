import os

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8765"))
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "changeme")

API_PORT = int(os.getenv("API_PORT", "8000"))

# Coordinate system: model trained on 1024x768
MODEL_WIDTH = 1024
MODEL_HEIGHT = 768
