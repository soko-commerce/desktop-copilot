"""
Direct VIDA search — deterministic flow with 2-4 Claude vision calls.

Workflow (per user specification):
1. Navigate to "Search Vehicle" page
2. Click "Clear All" to reset all fields
3. Fill in provided fields (VIN, model, year, etc.)
4. Click Search/Select
5. Extract results with Claude OCR

Total: 2-4 Claude calls per search (detect screen + optional re-detect + extract).
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
The VIDA application (Volvo diagnostic software) occupies the top ~52% of the screen.

Identify the VIDA screen state and provide EXACT pixel coordinates for visible UI elements.

Possible screens:
- "search_vehicle": The "Search Customer Vehicle Profile" page with empty/clearable fields
  (VIN, Model, Year, Partner Group, Engine, Transmission, Steering, Body Style, Special Vehicle).
  Has "Search" and "Clear All" buttons at bottom.
- "fine_tune": The "Fine-tune Vehicle" page — appears after a vehicle is identified.
  Similar fields but pre-filled. Has "Clear All" and "Select" buttons.
- "vehicle_selected": A vehicle is loaded, showing Quick Links and vehicle details.
- "parts_catalog": Parts catalog or search results page.
- "popup": A popup/dialog/modal is blocking (e.g., Release Notes).
- "unknown": Can't determine.

For each VISIBLE element, provide its CENTER coordinates in the 1024x768 image:
- "search_vehicle_tab": "Search Vehicle" text link in the top navigation bar
- "vin_field": The VIN text input field (the main text box, not the label)
- "vin_type_dropdown": The VIN type dropdown (next to VIN field, e.g. "Chassis No")
- "model_field": The Model dropdown/field
- "year_field": The Year dropdown/field
- "partner_group_field": The Partner Group dropdown
- "search_button": "Search" button (magnifying glass or text)
- "clear_all_button": "Clear All" button
- "select_button": "Select" button
- "close_popup": X button to close any popup/dialog
- "see_vehicle_details": "See Vehicle Details" link
- "hamburger_menu": Menu/hamburger icon in toolbar

Only include elements you can ACTUALLY SEE. Be precise — coordinates will be used for mouse clicks.

Respond with ONLY this JSON (no other text):
{
  "screen": "search_vehicle|fine_tune|vehicle_selected|parts_catalog|popup|unknown",
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
    await bridge_tools.get_dimensions(bridge)

    ss_b64 = await bridge_tools.screenshot(bridge)
    ss_bytes = base64.b64decode(ss_b64)

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

    try:
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
    """Click a named element using coordinates from Claude's detection."""
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
    await bridge_tools.key_press(bridge, "Escape")
    await asyncio.sleep(0.5)
    return True


async def go_to_search_vehicle(bridge: WebSocketBridge, detection: dict,
                                max_retries: int = 3) -> tuple[dict, int]:
    """Navigate to the Search Vehicle page and return detection + Claude calls used.

    Returns (detection_result, extra_claude_calls).
    """
    claude_calls = 0

    for attempt in range(max_retries):
        screen = detection.get("screen", "unknown")
        elements = detection.get("elements", {})

        logger.info(f"Navigation attempt {attempt+1}: screen={screen}")

        # Handle popups first
        if screen == "popup":
            await dismiss_popup(bridge, elements)
            detection, _ = await detect_screen(bridge)
            claude_calls += 1
            continue

        # Already on search_vehicle — ready
        if screen == "search_vehicle":
            return detection, claude_calls

        # On fine_tune or any other screen — click "Search Vehicle" tab to go to clean search
        if elements.get("search_vehicle_tab"):
            logger.info("Clicking 'Search Vehicle' tab to navigate to search page")
            await click_element(bridge, elements, "search_vehicle_tab")
            await asyncio.sleep(1.5)
            detection, _ = await detect_screen(bridge)
            claude_calls += 1
            continue

        # Fallback: click "Search Vehicle" tab at known approximate position
        # From screenshots: "Search Vehicle" text is at roughly x=20, y=35 in 1024x768
        logger.info("Using fallback click on 'Search Vehicle' tab area")
        await bridge_tools.left_click(bridge, 20, 35)
        await asyncio.sleep(1.5)
        detection, _ = await detect_screen(bridge)
        claude_calls += 1

    logger.warning(f"Could not navigate to search_vehicle after {max_retries} attempts")
    return detection, claude_calls


async def fill_field(bridge: WebSocketBridge, elements: dict,
                     field_name: str, value: str) -> bool:
    """Click a field, clear it, and type a value."""
    if not value:
        return False

    el = elements.get(field_name)
    if not el:
        logger.warning(f"Field '{field_name}' not found in detected elements")
        return False

    logger.info(f"Filling '{field_name}' with '{value}'")
    await click_element(bridge, elements, field_name)
    await asyncio.sleep(0.3)
    # Select all existing text and replace
    await bridge_tools.key_press(bridge, "ctrl+a")
    await asyncio.sleep(0.2)
    await bridge_tools.type_text(bridge, value)
    await asyncio.sleep(0.3)
    return True


async def execute_part_search(bridge: WebSocketBridge, query: str,
                              vin: str = "", model: str = "",
                              year: str = "") -> dict:
    """
    Execute a direct VIDA vehicle search following the user's workflow:
    1. Navigate to Search Vehicle page
    2. Click Clear All
    3. Fill in provided fields (VIN, model, year)
    4. Click Search/Select
    5. Extract results

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
        # --- Step 1: Detect current screen ---
        logger.info(f"Direct search starting: query='{query}', vin='{vin}', model='{model}', year='{year}'")
        detection, ss_bytes = await detect_screen(bridge)
        result["claude_calls"] += 1
        result["steps_taken"] += 1

        # --- Step 2: Navigate to Search Vehicle page ---
        detection, nav_calls = await go_to_search_vehicle(bridge, detection)
        result["claude_calls"] += nav_calls
        result["steps_taken"] += nav_calls

        screen = detection.get("screen", "unknown")
        elements = detection.get("elements", {})

        if screen not in ("search_vehicle", "fine_tune"):
            result["error"] = f"Failed to navigate to search page (stuck on '{screen}')"
            logger.error(result["error"])
            return result

        # --- Step 3: Click Clear All ---
        if elements.get("clear_all_button"):
            logger.info("Clicking 'Clear All' to reset fields")
            await click_element(bridge, elements, "clear_all_button")
            await asyncio.sleep(1.0)
            result["steps_taken"] += 1

            # Re-detect after clear to get fresh element positions
            detection, ss_bytes = await detect_screen(bridge)
            result["claude_calls"] += 1
            result["steps_taken"] += 1
            elements = detection.get("elements", {})
        else:
            logger.warning("'Clear All' button not found — proceeding with current field state")

        # --- Step 4: Fill in provided fields ---
        fields_filled = 0

        if vin:
            if await fill_field(bridge, elements, "vin_field", vin):
                fields_filled += 1
                result["steps_taken"] += 3
                # Tab out of VIN field to trigger any auto-lookup
                await bridge_tools.key_press(bridge, "Tab")
                await asyncio.sleep(0.5)

        if model:
            if await fill_field(bridge, elements, "model_field", model):
                fields_filled += 1
                result["steps_taken"] += 3

        if year:
            if await fill_field(bridge, elements, "year_field", year):
                fields_filled += 1
                result["steps_taken"] += 3

        if fields_filled == 0 and not query:
            result["error"] = "No fields to fill — provide VIN, model, year, or query"
            logger.error(result["error"])
            return result

        logger.info(f"Filled {fields_filled} fields")

        # --- Step 5: Click Search or Select ---
        clicked_action = False
        if elements.get("search_button"):
            logger.info("Clicking 'Search' button")
            await click_element(bridge, elements, "search_button")
            clicked_action = True
        elif elements.get("select_button"):
            logger.info("Clicking 'Select' button")
            await click_element(bridge, elements, "select_button")
            clicked_action = True
        else:
            # Try pressing Enter as fallback
            logger.info("No Search/Select button found — pressing Enter")
            await bridge_tools.key_press(bridge, "Return")
            clicked_action = True

        if clicked_action:
            result["steps_taken"] += 1
            await asyncio.sleep(3)  # Wait for VIDA to process

        # --- Step 6: Take final screenshot and extract results ---
        logger.info("Taking final screenshot for result extraction")
        final_b64 = await bridge_tools.screenshot(bridge)
        final_bytes = base64.b64decode(final_b64)
        result["steps_taken"] += 1

        # --- Step 7: Extract results with Claude OCR ---
        logger.info("Extracting results with Claude vision")
        raw_text = await extract_results_with_claude(final_bytes, query)
        result["claude_calls"] += 1
        result["raw_response"] = raw_text

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
