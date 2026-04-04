import os

# AI strategy: "anthropic" or "gemini"
AI_STRATEGY = os.getenv("AI_STRATEGY", "anthropic")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.1"))

BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8765"))
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "changeme")

API_PORT = int(os.getenv("API_PORT", "8000"))

# Direct piglet connection (bypasses WebSocket tunnel for local dev)
# Set to e.g. "http://localhost:3000" when running piglet locally
PIGLET_DIRECT_URL = os.getenv("PIGLET_DIRECT_URL", "")

# Coordinate system: model trained on 1024x768
MODEL_WIDTH = 1024
MODEL_HEIGHT = 768

# Agent context window: keep only last N messages to avoid cost explosion
# Higher values = more context for the agent but more expensive
AGENT_CONTEXT_WINDOW = int(os.getenv("AGENT_CONTEXT_WINDOW", "20"))
