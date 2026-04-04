"""
VIDA AI agent — two execution modes:

1. SCRIPTED MODE (default): Follow pre-defined workflow steps using screen
   detection (perceptual hashing). Zero Claude calls on the happy path.
   Falls back to AI mode on unexpected screens.

2. AI MODE (fallback): Vision agent with screenshot/click/type tools via
   LangGraph. Supports both Anthropic Claude and Google Gemini.
"""

import base64
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, MessagesState, END

from bridge.ws_bridge import WebSocketBridge
from config import (
    AI_STRATEGY,
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_TEMPERATURE,
    AGENT_CONTEXT_WINDOW,
)
from agent import tools as bridge_tools
from agent.vida_prompts import VIDA_SYSTEM_PROMPT
from agent.screen_detector import ScreenDetector
from agent.vida_workflows import WorkflowExecutor
from agent.workflow_cache import WorkflowCache

logger = logging.getLogger(__name__)

# Shared instances (initialized once per process)
_screen_detector = ScreenDetector()
_workflow_cache = WorkflowCache()


def get_screen_detector() -> ScreenDetector:
    return _screen_detector


def get_workflow_cache() -> WorkflowCache:
    return _workflow_cache


def _build_llm():
    """Build the LLM based on AI_STRATEGY config."""
    if AI_STRATEGY == "gemini" and GEMINI_API_KEY:
        from langchain_google_genai import ChatGoogleGenerativeAI
        logger.info(f"Using Gemini model: {GEMINI_MODEL}")
        return ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            google_api_key=GEMINI_API_KEY,
            temperature=GEMINI_TEMPERATURE,
            max_output_tokens=4096,
        )
    else:
        from langchain_anthropic import ChatAnthropic
        logger.info(f"Using Anthropic model: {ANTHROPIC_MODEL}")
        return ChatAnthropic(
            model=ANTHROPIC_MODEL,
            api_key=ANTHROPIC_API_KEY,
            max_tokens=4096,
            temperature=0.1,
        )


async def run_scripted_search(bridge: WebSocketBridge, query: str) -> dict:
    """
    Try the scripted workflow first (0 Claude calls on happy path).

    Returns:
        {
            "success": bool,
            "screenshot_bytes": bytes | None,  # final screenshot for OCR extraction
            "completed_steps": int,
            "failure_reason": str | None,
        }
    """
    executor = WorkflowExecutor(bridge, _screen_detector, cache=_workflow_cache)
    return await executor.execute_part_search(query)


async def extract_results_with_claude(screenshot_bytes: bytes, query: str) -> str:
    """
    Use vision LLM on a SINGLE screenshot to extract parts data.
    This is the one LLM call we make — reading the search results table.
    """
    llm = _build_llm()

    b64 = base64.b64encode(screenshot_bytes).decode()
    messages = [
        SystemMessage(content=(
            "You are reading a VIDA (Volvo diagnostic software) search results screen. "
            "Extract ALL part numbers, descriptions, quantities, and any other visible data "
            "from the results table. Return the data as a JSON array:\n"
            '[{"partNumber": "...", "description": "...", "found": true, "notes": "..."}]\n'
            "Only return the JSON array, no other text."
        )),
        HumanMessage(content=[
            {"type": "text", "text": f"Extract all parts data from this VIDA search results screen for query: {query}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]

    response = await llm.ainvoke(messages)
    return response.content if isinstance(response.content, str) else str(response.content)


def _trim_messages(messages: list, window: int) -> list:
    """
    Keep only the system prompt + last `window` messages.
    Drops old screenshots from context to prevent cost explosion.
    """
    if len(messages) <= window + 1:
        return messages

    # Always keep system message at index 0
    system = []
    rest = messages
    if messages and isinstance(messages[0], SystemMessage):
        system = [messages[0]]
        rest = messages[1:]

    # Keep only last `window` messages
    trimmed = rest[-window:]
    return system + trimmed


def build_vida_agent(bridge: WebSocketBridge):
    """Build a full LangGraph agent (AI mode fallback) wired to the given bridge."""

    @tool
    async def screenshot() -> str:
        """Take a screenshot of the screen. Returns base64-encoded PNG image."""
        return await bridge_tools.screenshot(bridge)

    @tool
    async def get_dimensions() -> str:
        """Get the screen dimensions."""
        return await bridge_tools.get_dimensions(bridge)

    @tool
    async def type_text(text: str) -> str:
        """Type text at the current cursor position. Does NOT press Enter automatically."""
        return await bridge_tools.type_text(bridge, text)

    @tool
    async def key_press(combo: str) -> str:
        """Press a key or key combination. Examples: 'Return', 'Tab', 'ctrl+c', 'alt+Tab'."""
        return await bridge_tools.key_press(bridge, combo)

    @tool
    async def left_click(x: int, y: int) -> str:
        """Left-click at the given coordinates (1024x768 coordinate space)."""
        return await bridge_tools.left_click(bridge, x, y)

    @tool
    async def right_click(x: int, y: int) -> str:
        """Right-click at the given coordinates (1024x768 coordinate space)."""
        return await bridge_tools.right_click(bridge, x, y)

    @tool
    async def double_click(x: int, y: int) -> str:
        """Double-click at the given coordinates (1024x768 coordinate space)."""
        return await bridge_tools.double_click(bridge, x, y)

    @tool
    async def mouse_move(x: int, y: int) -> str:
        """Move the mouse to the given coordinates (1024x768 coordinate space)."""
        return await bridge_tools.mouse_move(bridge, x, y)

    @tool
    async def cursor_position() -> str:
        """Get the current cursor position in 1024x768 coordinate space."""
        return await bridge_tools.cursor_position(bridge)

    all_tools = [
        screenshot, get_dimensions, type_text, key_press,
        left_click, right_click, double_click, mouse_move, cursor_position,
    ]
    tool_map = {t.name: t for t in all_tools}

    llm = _build_llm().bind_tools(all_tools)

    async def call_model(state: MessagesState):
        messages = state["messages"]
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=VIDA_SYSTEM_PROMPT)] + messages

        # Trim old messages to keep costs down
        messages = _trim_messages(messages, AGENT_CONTEXT_WINDOW)

        response = await llm.ainvoke(messages)
        return {"messages": [response]}

    async def run_tools(state: MessagesState):
        last_msg = state["messages"][-1]
        results = []
        for tc in last_msg.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                results.append(
                    ToolMessage(content=f"Unknown tool: {tc['name']}", tool_call_id=tc["id"])
                )
                continue
            try:
                logger.info(f"Tool call: {tc['name']}({tc['args']})")
                result = await tool_fn.ainvoke(tc["args"])
                if tc["name"] == "screenshot" and not result.startswith("Error"):
                    content = [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{result}"}},
                    ]
                else:
                    content = result
                results.append(ToolMessage(content=content, tool_call_id=tc["id"]))
            except Exception as e:
                logger.error(f"Tool {tc['name']} failed: {e}")
                results.append(ToolMessage(content=f"Error: {str(e)}", tool_call_id=tc["id"]))
        return {"messages": results}

    def should_continue(state: MessagesState) -> Literal["tools", "end"]:
        last_msg = state["messages"][-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "tools"
        return "end"

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", run_tools)
    graph.set_entry_point("model")
    graph.add_conditional_edges("model", should_continue, {"tools": "tools", "end": END})
    graph.add_edge("tools", "model")

    return graph.compile()
