"""
Direct VIDA search — deterministic flow with 2-3 Claude vision calls.

Instead of an agent loop (10-30 calls), this module:
1. Takes a screenshot
2. Asks Claude: "What screen is this? Where are the UI elements?" (1 call)
3. Executes a fixed click/type sequence based on the detected screen
4. Takes a final screenshot
5. Asks Claude: "Extract the parts data from this results screen" (1 call)

Total: 2-3 Claude calls per search.
"""

import asyncio
import base64
import json
import logging
from typing import Optional

from bridge.ws_bridge import WebSocketBridge
from agent import tools as bridge_tools
from agent.vida_agent import _build_llm, extract_results_with_claude

logger = logging.getLogger(__name__)

DETECT_SCREEN_PROMPT = """You are analyzing a 1024x768 screenshot of a Windows desktop.
The VIDA application (Volvo diagnostic software) is in the TOP PORTION of the screen.

Look at the screenshot and tell me:
1. What VIDA screen/state is currently showing?
2. The EXACT pixel coordinates (x, y) of key UI elements visible in the screenshot.

Possible screens:
- "home": VIDA home screen / Search Customer Vehicle Profile with VIN fields
- "vehicle_selected": A vehicle is loaded, showing vehicle details and Quick Links
- "search_vehicle": The search/fine-tune vehicle form
- "parts_catalog": Parts catalog or search page
- "search_results": Search results showing parts list/table
- "menu_open": A dropdown menu or hamburger menu is open
- "popup": A popup/dialog/modal is blocking (e.g., Release Notes)
- "unknown": Can't determine

For each visible element, provide its CENTER coordinates in the 1024x768 image:
- "vin_field": The VIN text input field
- "search_button": Search or magnifying glass icon button
- "clear_all": Clear All button
- "select_button": Select button
- "hamburger_menu": Menu/hamburger icon
- "search_field": Parts search/filter text field
- "close_popup": X button or close for any popup
- "search_vehicle_tab": "Search Vehicle" navigation link
- "home_tab": Home tab
- "parts_catalog_link": Any link to Parts Catalog
- "popup_close_area": Area to click to dismiss a popup

Only include elements you can actually see. Be precise with coordinates — they will be used for mouse clicks.

Respond with ONLY this JSON (no other text):
{
  "screen": "home|vehicle_selected|search_vehicle|parts_catalog|search_results|menu_open|popup|unknown",
  "elements": {
    "element_name": {"x": 123, "y": 456},
    ...
  },
  "notes": "brief description of what you see"
}"""


async def detect_screen(bridge: WebSocketBridge) -> tuple[dict, bytes]:
    """Take a screenshot and ask Claude to identify the screen and elements.

    Returns (detection_result, screenshot_bytes).
    """
    # Ensure dimensions are loaded
    await bridge_tools.get_dimensions(bridge)

    # Take screenshot
    ss_b64 = await bridge_tools.screenshot(bridge)
    ss_bytes = base64.b64decode(ss_b64)

    # Ask Claude to detect screen state
    llm = _build_llm()
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=DETECT_SCREEN_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": "Analyze this VIDA screenshot. Return the JSON with screen state and element coordinates."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ss_b64}"}},
        ]),
    ]

    response = await llm.ainvoke(messages)
    text = response.content if isinstance(response.content, str) else str(response.content)

    # Parse JSON from response
    try:
        # Handle markdown code blocks
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())
    except json.JSONDecodeError:
        logger.error(f"Failed to parse Claude screen detection: {text}")
        result = {"screen": "unknown", "elements": {}, "notes": text[:200]}

    logger.info(f"Screen detected: {result.get('screen')} — {result.get('notes', '')}")
    return result, ss_bytes


async def click_element(bridge: WebSocketBridge, elements: dict, name: str,
                        fallback_x: int = 0, fallback_y: int = 0) -> bool:
    """Click a named element using coordinates from Claude's detection.

    Uses model coordinates (1024x768 space) which go through _to_screen().
    Falls back to provided defaults if element not found.
    """
    el = elements.get(name)
    if el:
        x, y = el["x"], el["y"]
    elif fallback_x or fallback_y:
        x, y = fallback_x, fallback_y
        logger.warning(f"Element '{name}' not found, using fallback ({x}, {y})")
    else:
        logger.error(f"Element '{name}' not found and no fallback provided")
        return False

    logger.info(f"Clicking '{name}' at model ({x}, {y})")
    await bridge_tools.left_click(bridge, x, y)
    return True


async def dismiss_popup(bridge: WebSocketBridge, elements: dict) -> bool:
    """Try to dismiss any popup/dialog."""
    if elements.get("close_popup"):
        await click_element(bridge, elements, "close_popup")
        await asyncio.sleep(0.5)
        return True
    # Try Escape
    await bridge_tools.key_press(bridge, "Escape")
    await asyncio.sleep(0.5)
    return True


async def navigate_to_search(bridge: WebSocketBridge, detection: dict, max_retries: int = 3) -> dict:
    """Navigate VIDA to a state where we can search for parts.

    Returns the final screen detection result.
    """
    for attempt in range(max_retries):
        screen = detection.get("screen", "unknown")
        elements = detection.get("elements", {})

        logger.info(f"Navigate attempt {attempt+1}: screen={screen}")

        if screen == "popup":
            await dismiss_popup(bridge, elements)
            detection, _ = await detect_screen(bridge)
            continue

        if screen == "search_results":
            # Already on search results — good to go
            return detection

        if screen == "parts_catalog":
            # Already on parts catalog — good to go
            return detection

        if screen in ("home", "vehicle_selected", "search_vehicle"):
            # Try to find parts catalog or search
            if elements.get("parts_catalog_link"):
                await click_element(bridge, elements, "parts_catalog_link")
                await asyncio.sleep(2)
                detection, _ = await detect_screen(bridge)
                continue

            if elements.get("hamburger_menu"):
                await click_element(bridge, elements, "hamburger_menu")
                await asyncio.sleep(1)
                detection, _ = await detect_screen(bridge)
                continue

            if elements.get("search_button"):
                await click_element(bridge, elements, "search_button")
                await asyncio.sleep(1)
                detection, _ = await detect_screen(bridge)
                continue

            # If we're on search_vehicle with VIN field, we can search here
            if screen == "search_vehicle" and elements.get("vin_field"):
                return detection

            # If on home with VIN field, we can start from here
            if elements.get("vin_field"):
                return detection

        if screen == "menu_open":
            if elements.get("parts_catalog_link"):
                await click_element(bridge, elements, "parts_catalog_link")
                await asyncio.sleep(2)
                detection, _ = await detect_screen(bridge)
                continue

        # If we can't figure out what to do, try clicking away
        await bridge_tools.key_press(bridge, "Escape")
        await asyncio.sleep(0.5)
        detection, _ = await detect_screen(bridge)

    return detection


async def execute_part_search(bridge: WebSocketBridge, query: str,
                              vin: str = "") -> dict:
    """
    Execute a direct VIDA part search with 2-3 Claude vision calls.

    Returns:
        {
            "success": bool,
            "parts": list[dict],
            "raw_response": str,
            "steps_taken": int,
            "claude_calls": int,
            "error": str,
        }
    """
    result = {
        "success": False,
        "parts": [],
        "raw_response": "",
        "steps_taken": 0,
        "claude_calls": 0,
        "error": "",
    }

    try:
        # Step 1: Detect current screen (1 Claude call)
        logger.info(f"Direct search starting: query='{query}', vin='{vin}'")
        detection, ss_bytes = await detect_screen(bridge)
        result["claude_calls"] += 1
        result["steps_taken"] += 1

        screen = detection.get("screen", "unknown")
        elements = detection.get("elements", {})

        # Step 2: Handle popups
        if screen == "popup":
            await dismiss_popup(bridge, elements)
            detection, ss_bytes = await detect_screen(bridge)
            result["claude_calls"] += 1
            result["steps_taken"] += 1
            screen = detection.get("screen", "unknown")
            elements = detection.get("elements", {})

        # Step 3: Navigate to a searchable state if needed
        if screen not in ("parts_catalog", "search_results"):
            detection = await navigate_to_search(bridge, detection)
            screen = detection.get("screen", "unknown")
            elements = detection.get("elements", {})

        # Step 4: If we have a VIN and we're on a screen with VIN field, enter it
        if vin and elements.get("vin_field"):
            logger.info(f"Entering VIN: {vin}")
            await click_element(bridge, elements, "vin_field")
            await asyncio.sleep(0.3)
            await bridge_tools.key_press(bridge, "ctrl+a")
            await asyncio.sleep(0.2)
            await bridge_tools.type_text(bridge, vin)
            await asyncio.sleep(0.3)
            result["steps_taken"] += 3

        # Step 5: If we have a search field, enter the query
        if elements.get("search_field"):
            logger.info(f"Entering search query: {query}")
            await click_element(bridge, elements, "search_field")
            await asyncio.sleep(0.3)
            await bridge_tools.key_press(bridge, "ctrl+a")
            await asyncio.sleep(0.2)
            await bridge_tools.type_text(bridge, query)
            await asyncio.sleep(0.3)
            await bridge_tools.key_press(bridge, "Return")
            await asyncio.sleep(2)  # Wait for search results
            result["steps_taken"] += 4
        elif elements.get("select_button"):
            # On vehicle search page — click Select to load vehicle first
            logger.info("Clicking Select to load vehicle")
            await click_element(bridge, elements, "select_button")
            await asyncio.sleep(3)
            result["steps_taken"] += 1
            # Re-detect screen after selection
            detection, ss_bytes = await detect_screen(bridge)
            result["claude_calls"] += 1
            screen = detection.get("screen", "unknown")
            elements = detection.get("elements", {})

        # Step 6: Take final screenshot and extract results
        logger.info("Taking final screenshot for result extraction")
        final_b64 = await bridge_tools.screenshot(bridge)
        final_bytes = base64.b64decode(final_b64)
        result["steps_taken"] += 1

        # Step 7: Extract results with Claude OCR (1 Claude call)
        logger.info("Extracting results with Claude vision")
        raw_text = await extract_results_with_claude(final_bytes, query)
        result["claude_calls"] += 1
        result["raw_response"] = raw_text

        # Parse results
        from agent.result_parser import parse_agent_response
        parts_data = parse_agent_response(raw_text)
        result["parts"] = parts_data
        result["success"] = True

        logger.info(f"Direct search complete: {len(parts_data)} parts found, "
                     f"{result['claude_calls']} Claude calls, {result['steps_taken']} steps")

    except Exception as e:
        logger.error(f"Direct search failed: {e}", exc_info=True)
        result["error"] = str(e)

    return result
