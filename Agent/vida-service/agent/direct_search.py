"""
Direct VIDA search — deterministic flow with 2-6 Claude vision calls.

Two modes:
A) VIN/model provided: Navigate to Search Vehicle → fill fields → Search → Select
   → navigate to Parts → search for query within vehicle catalog
B) Query only (no VIN/model): Detect current screen — if a vehicle is already loaded,
   search its parts catalog directly.

Total: 2-6 Claude calls per search (detect screen + navigation + extract).
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

DETECT_SCREEN_PROMPT = """You are analyzing a screenshot of the VIDA application (Volvo diagnostic software).
Your job is to identify the screen state and provide EXACT pixel coordinates for visible UI elements.
Coordinates will be used for automated mouse clicks — precision matters.

## Screen States
- "search_vehicle": The "Search Customer Vehicle Profile" page. Has form fields (VIN, Model, Year, etc.)
  and "Search" / "Clear All" buttons. Fields may be empty or filled from a previous search.
- "fine_tune": The "Fine-tune Vehicle" page — similar layout but with pre-filled fields and a "Select" button.
- "vehicle_selected": A vehicle is loaded — shows Work Lists / Parts Lists tabs, left sidebar with
  Planning/Parts/Diagnostics sections. Vehicle info shown in header (VIN, Model/Year).
- "parts_catalog": The Parts page — shows category tree (e.g., "2 Engine", "5 Brakes") with a search
  field labeled "Part number or description" at top, or a parts search results table.
- "popup": A popup/dialog/modal is blocking the main content.
- "unknown": Can't determine the current state.

## Elements to Detect
For each VISIBLE element, provide its CENTER coordinates as {"x": N, "y": N}.

### On search_vehicle / fine_tune screens:
- "search_vehicle_tab": The "Search Vehicle" text link in the navigation bar below the VIDA title bar
- "vin_field": The FIRST (leftmost) text input field in the VIN row. IMPORTANT: The VIN row has TWO
  adjacent input fields — a wider one on the LEFT for the VIN number, and a narrower one on the RIGHT
  labeled "Chassis No". You MUST return the LEFT field's center, NOT the right one or the center of both.
- "chassis_no_field": The second (right) input in the VIN row, labeled "Chassis No"
- "model_field": The Model dropdown (below VIN, left side)
- "year_field": The Year dropdown (next to Model)
- "partner_group_field": The Partner Group dropdown
- "search_button": The "Search" button (bottom-left area of the form, usually left of "Clear All")
- "clear_all_button": The "Clear All" button (bottom area of the form, usually right of "Search")
- "select_button": The "Select" button (appears on fine_tune or after search results)
- "see_vehicle_details": "See Vehicle Details" link (appears after a VIN search finds a vehicle)

### On vehicle_selected screen:
- "parts_sidebar": The "Parts" link in the left sidebar navigation panel
- "search_vehicle_tab": "Search Vehicle" tab in top navigation

### On parts_catalog screen:
- "parts_search_field": The text input field labeled "Part number or description"
- "parts_search_icon": The search/magnifying glass button next to the search field
- "search_vehicle_tab": "Search Vehicle" tab in top navigation

### Always check for:
- "close_popup": X button to close any popup/dialog/modal

Only include elements you can ACTUALLY SEE. Do not guess at hidden elements.

## Response Format
Return ONLY this JSON (no markdown, no explanation):
{
  "screen": "search_vehicle|fine_tune|vehicle_selected|parts_catalog|popup|unknown",
  "elements": {
    "element_name": {"x": 123, "y": 456}
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

    logger.info(f"Filling '{field_name}' with '{value}' at ({el['x']}, {el['y']})")
    await click_element(bridge, elements, field_name)
    await asyncio.sleep(0.3)
    # Select all existing text and replace
    await bridge_tools.key_press(bridge, "ctrl+a")
    await asyncio.sleep(0.2)
    await bridge_tools.type_text(bridge, value)
    await asyncio.sleep(0.3)
    return True


async def _search_parts_catalog(bridge: WebSocketBridge, query: str,
                                 result: dict) -> dict:
    """Navigate to Parts section and search for a query within the loaded vehicle's catalog.

    Assumes we're on vehicle_selected or parts_catalog screen.
    Mutates and returns `result`.
    """
    detection, ss_bytes = await detect_screen(bridge)
    result["claude_calls"] += 1
    result["steps_taken"] += 1
    screen = detection.get("screen", "unknown")
    elements = detection.get("elements", {})

    # If on vehicle_selected, navigate to Parts sidebar first
    if screen == "vehicle_selected":
        if await click_element(bridge, elements, "parts_sidebar"):
            logger.info("Navigated to Parts section")
            result["steps_taken"] += 1
            await asyncio.sleep(2)

            # Re-detect to find parts search field
            detection, ss_bytes = await detect_screen(bridge)
            result["claude_calls"] += 1
            result["steps_taken"] += 1
            elements = detection.get("elements", {})
        else:
            logger.warning("'Parts' sidebar link not found — extracting from current screen")

    # Now search within the parts catalog
    if query and elements.get("parts_search_field"):
        if await fill_field(bridge, elements, "parts_search_field", query):
            logger.info(f"Searching Parts catalog for: {query}")
            result["steps_taken"] += 3

            # Click search icon or press Enter
            if await click_element(bridge, elements, "parts_search_icon"):
                result["steps_taken"] += 1
            else:
                await bridge_tools.key_press(bridge, "Return")
                result["steps_taken"] += 1
            await asyncio.sleep(4)  # Wait for parts results

    return result


async def execute_part_search(bridge: WebSocketBridge, query: str,
                              vin: str = "", model: str = "",
                              year: str = "") -> dict:
    """
    Execute a direct VIDA search.

    Two modes:
    A) VIN/model provided: Navigate to Search Vehicle → fill fields → Search → Select
       → navigate to Parts → search for query within vehicle catalog
    B) Query only (no VIN/model): Detect current screen — if a vehicle is already loaded,
       search its parts catalog directly. No vehicle re-search needed.

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
        has_vehicle_fields = bool(vin or model or year)
        logger.info(f"Direct search starting: query='{query}', vin='{vin}', model='{model}', year='{year}'")

        # --- Step 1: Detect current screen ---
        detection, ss_bytes = await detect_screen(bridge)
        result["claude_calls"] += 1
        result["steps_taken"] += 1
        screen = detection.get("screen", "unknown")

        # --- Mode B: Query only — search within already-loaded vehicle ---
        if not has_vehicle_fields and query:
            if screen in ("vehicle_selected", "parts_catalog"):
                logger.info(f"Vehicle already loaded (screen={screen}), searching parts catalog for: {query}")
                result = await _search_parts_catalog(bridge, query, result)
            else:
                result["error"] = (
                    f"No vehicle loaded (screen='{screen}'). "
                    "Load a vehicle first by searching with a VIN or model."
                )
                logger.error(result["error"])
                return result

        # --- Mode A: VIN/model search — full vehicle load flow ---
        else:
            # Navigate to Search Vehicle page
            detection, nav_calls = await go_to_search_vehicle(bridge, detection)
            result["claude_calls"] += nav_calls
            result["steps_taken"] += nav_calls

            screen = detection.get("screen", "unknown")
            elements = detection.get("elements", {})

            if screen not in ("search_vehicle", "fine_tune"):
                result["error"] = f"Failed to navigate to search page (stuck on '{screen}')"
                logger.error(result["error"])
                return result

            # Click Clear All
            if await click_element(bridge, elements, "clear_all_button"):
                logger.info("Clicked 'Clear All' to reset fields")
                await asyncio.sleep(1.0)
                result["steps_taken"] += 1

                # Re-detect after clear to get fresh element positions
                detection, ss_bytes = await detect_screen(bridge)
                result["claude_calls"] += 1
                result["steps_taken"] += 1
                elements = detection.get("elements", {})
            else:
                logger.warning("'Clear All' button not found — proceeding with current field state")

            # Fill in provided fields
            fields_filled = 0

            if vin:
                if await fill_field(bridge, elements, "vin_field", vin):
                    fields_filled += 1
                    result["steps_taken"] += 3
                    # Tab out of VIN field to trigger auto-lookup
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

            # Click Search
            if not await click_element(bridge, elements, "search_button"):
                if not await click_element(bridge, elements, "select_button"):
                    logger.info("No Search/Select button found — pressing Enter")
                    await bridge_tools.key_press(bridge, "Return")
            result["steps_taken"] += 1
            await asyncio.sleep(3)  # Wait for VIDA to process

            # For VIN search, complete the full flow:
            #     Search → re-detect → Select → re-detect → Parts → search parts
            if vin:
                # Re-detect to find Select button on the vehicle results screen
                detection, ss_bytes = await detect_screen(bridge)
                result["claude_calls"] += 1
                result["steps_taken"] += 1
                elements = detection.get("elements", {})

                # Click Select to load the vehicle profile
                if await click_element(bridge, elements, "select_button"):
                    logger.info("VIN search: clicked 'Select' to load vehicle profile")
                    result["steps_taken"] += 1
                    await asyncio.sleep(4)  # Wait for vehicle profile to load

                    # Search within the vehicle's parts catalog
                    if query:
                        result = await _search_parts_catalog(bridge, query, result)
                else:
                    logger.warning("'Select' button not found after VIN search — extracting from current screen")

        # --- Final: Take screenshot and extract results ---
        logger.info("Taking final screenshot for result extraction")
        final_b64 = await bridge_tools.screenshot(bridge)
        final_bytes = base64.b64decode(final_b64)
        result["steps_taken"] += 1

        # Extract results with Claude OCR
        logger.info("Extracting results with Claude vision")
        raw_text = await extract_results_with_claude(final_bytes, query or vin)
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
