"""
VIDA parts catalog browsing — visual navigation with Claude vision.

Two-phase operation:
1. get_catalog_categories(bridge, query) — navigate to Parts category tree, extract categories
2. browse_category(bridge, category_name, query) — drill into a category, extract parts

Uses 2-4 Claude vision calls per phase.
"""

import asyncio
import base64
import json
import logging

from bridge.ws_bridge import WebSocketBridge
from agent import tools as bridge_tools
from agent.vida_agent import _build_llm, extract_results_with_claude
from agent.result_parser import parse_agent_response

logger = logging.getLogger(__name__)

# Single prompt that handles navigation + extraction in one vision call
NAVIGATE_AND_EXTRACT_PROMPT = """You are analyzing a 1024x768 screenshot of the VIDA application (Volvo diagnostic software).
A vehicle is loaded and the "Parts" section should be visible in the left sidebar.

Your task: determine the current state and what action is needed to reach the TOP-LEVEL parts catalog category tree.

The VIDA parts catalog has a category tree with numbered categories like:
"1 Engine with mountings and equipment", "2 Fuel system, exhaust system", "3 Power transmission",
"4 Brakes", "5 Suspension and steering", "6 Body", "7 Electrical system", etc.

Analyze the screenshot and determine which state we are in:

**State A — Category tree IS visible**: The main content area shows numbered categories (1 Engine..., 2 Fuel..., etc.)
In this case, extract all categories.

**State B — Parts list showing (inside a specific category)**: The main area shows a parts table with part numbers.
There should be a "Back to catalogue" or similar link to go back to the category tree.
In this case, provide the click coordinates for the "Back to catalogue" link.

**State C — "Parts" not selected in sidebar**: The left sidebar shows items like Lists, Planning, Parts, Software, etc.
and "Parts" is NOT highlighted/selected.
In this case, provide the click coordinates for the "Parts" item in the sidebar.

**State D — No vehicle loaded**: We're on the Search Vehicle page.
In this case, indicate no vehicle.

Return ONLY this JSON (no other text):
{{
  "state": "A|B|C|D",
  "categories": [
    {{"name": "exact category text", "y": 123, "relevance": "high|medium|low", "reason": "brief explanation"}}
  ],
  "action_needed": {{
    "description": "what to click",
    "target": {{"x": 123, "y": 456}}
  }},
  "notes": "brief description of what you see"
}}

Rules:
- If state is A: populate "categories" with ALL numbered categories, set "action_needed" to null
- If state is B/C: set "categories" to empty [], provide "action_needed" with click target
- If state is D: set both to null/empty
- "y" is the vertical CENTER coordinate of the category text in the 1024x768 image
- Assess relevance for the user's query: "{query}"
- Sort categories by visual position (top to bottom)
"""

CATALOG_EXTRACT_PROMPT = """You are analyzing a 1024x768 screenshot of the VIDA application (Volvo diagnostic software).
The main content area shows the parts catalog category tree.

Extract ONLY the TOP-LEVEL categories (the main numbered groups).
Top-level categories have single-digit or low numbers like:
  "1 Engine with mountings and equipment", "2 Fuel system, exhaust system",
  "3 Electrical system", "4 Power transmission", "5 Brakes",
  "6 Suspension and steering", "7 Body and interior", etc.

DO NOT extract subcategories (like "211 Cylinder head", "51 Wheel brake").
The top-level ones have a ► or ▲ arrow and start with a SINGLE DIGIT (1-9).

For each category, assess how likely it is to contain a part described as: "{query}"

Return ONLY this JSON (no other text):
{{
  "categories": [
    {{"name": "exact category text as shown", "y": 123, "relevance": "high|medium|low", "reason": "brief explanation"}},
    ...
  ],
  "view_state": "categories_visible|no_categories|parts_list|unknown",
  "needs_scroll_up": true
}}

Rules:
- "y" is the vertical CENTER coordinate of the category text in the 1024x768 image
- Include ONLY top-level categories (single digit prefix like "3 Electrical system")
- Sort by visual position (top to bottom)
- Set "needs_scroll_up" to true if the tree seems scrolled down (category 1 is not visible)
- If you see a parts table instead of a category tree, set view_state to "parts_list"
"""

CATEGORY_LOCATE_PROMPT = """You are analyzing a 1024x768 screenshot of the VIDA application.
The main content area shows a parts catalog category tree.

Find the category "{category_name}" (or closest match) and provide click coordinates.
If the category is already expanded (has a ▲ arrow or visible child items indented below it),
list ALL its visible subcategories.

The user is looking for: "{query}"
Think carefully about which subcategory is MOST LIKELY to contain this specific part.
For example:
- "brake pads" → subcategory about wheel brakes / disc brakes, NOT brake lines or master cylinder
- "front beam" / "subframe" → subcategory about suspension frame / chassis
- "headlight" → subcategory about front lighting

Return ONLY this JSON (no other text):
{{
  "found": true,
  "target": {{"x": 123, "y": 456}},
  "expanded": false,
  "subcategories": [
    {{"name": "subcategory text", "x": 130, "y": 180, "relevance": "high|medium|low", "reason": "why this subcategory does or does not contain {query}"}}
  ]
}}

If the category is NOT visible, return:
{{
  "found": false,
  "target": null,
  "expanded": false,
  "subcategories": []
}}
"""


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude response, handling markdown code blocks and preamble text."""
    try:
        # Try 1: code block
        if "```" in text:
            block = text.split("```")[1]
            if block.startswith("json"):
                block = block[4:]
            return json.loads(block.strip())

        # Try 2: direct JSON
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try 3: find the first { and last } — extract embedded JSON
    try:
        first_brace = text.index("{")
        last_brace = text.rindex("}")
        candidate = text[first_brace:last_brace + 1]
        return json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        logger.error(f"Failed to parse JSON response: {text[:500]}")
        return {}


async def _vision_call(bridge: WebSocketBridge, prompt: str, user_text: str, ss_b64: str = None) -> tuple[dict, str]:
    """Make a Claude vision call with a screenshot. Returns (parsed_json, raw_b64)."""
    if not ss_b64:
        ss_b64 = await bridge_tools.screenshot(bridge)

    llm = _build_llm()
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=prompt),
        HumanMessage(content=[
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ss_b64}"}},
        ]),
    ]

    response = await llm.ainvoke(messages)
    text = response.content if isinstance(response.content, str) else str(response.content)
    return _parse_json_response(text), ss_b64


async def get_catalog_categories(bridge: WebSocketBridge, query: str) -> dict:
    """
    Navigate to Parts category tree and extract categories.

    Handles multiple starting states:
    - Already on category tree → extract categories
    - Inside a parts list → click "Back to catalogue" → extract categories
    - Parts not selected → click "Parts" → then navigate to tree

    Returns:
        {
            "success": bool,
            "categories": [{"name": str, "relevance": str, "reason": str}],
            "claude_calls": int,
            "error": str,
        }
    """
    result = {"success": False, "categories": [], "claude_calls": 0, "error": ""}

    try:
        # Step 1: Take screenshot and analyze state
        prompt = NAVIGATE_AND_EXTRACT_PROMPT.format(query=query)
        data, ss_b64 = await _vision_call(bridge, prompt, f"Analyze this VIDA screenshot. User is looking for: {query}")
        result["claude_calls"] += 1

        if not data:
            result["error"] = "Could not analyze VIDA screenshot"
            return result

        state = data.get("state", "unknown")
        logger.info(f"Catalog browse: VIDA state = {state}, notes = {data.get('notes', '')}")

        if state == "D":
            result["error"] = "No vehicle loaded. Please search for a vehicle first (by VIN or model) before browsing the parts catalog."
            return result

        # State A: categories already visible — extract them directly
        if state == "A" and data.get("categories"):
            categories = data["categories"]
            result["success"] = True
            result["categories"] = [
                {"name": c["name"], "relevance": c.get("relevance", ""), "reason": c.get("reason", "")}
                for c in categories
            ]
            logger.info(f"Extracted {len(categories)} categories directly (state A), {result['claude_calls']} Claude calls")
            return result

        # State B or C: need to navigate first
        if state == "B":
            # "Back to catalogue" link has a fixed position in VIDA:
            # Center panel, just below "Parts" heading — model coords ~(210, 176)
            logger.info("Clicking 'Back to catalogue' at hardcoded position (210, 176)")
            await bridge_tools.left_click(bridge, 210, 176)
            await asyncio.sleep(2.5)
        elif state == "C":
            # State C: use Claude-detected coordinates for the "Parts" sidebar link
            action = data.get("action_needed")
            if not action or not action.get("target"):
                result["error"] = f"VIDA is in state '{state}' but no navigation action could be determined"
                return result
            target = action["target"]
            logger.info(f"Clicking '{action.get('description', 'nav target')}' at ({target['x']}, {target['y']})")
            await bridge_tools.left_click(bridge, target["x"], target["y"])
            await asyncio.sleep(2.5)
        else:
            result["error"] = f"Unexpected VIDA state: '{state}'"
            return result

        # Step 2: Take a fresh screenshot and extract categories.
        # No scrolling — Page_Up/Down navigates tree items (not the view).
        # We extract whatever top-level categories are visible.
        prompt = NAVIGATE_AND_EXTRACT_PROMPT.format(query=query)
        data, ss_b64 = await _vision_call(
            bridge, prompt,
            f"Analyze this VIDA screenshot after navigation. User is looking for: {query}"
        )
        result["claude_calls"] += 1

        if not data:
            result["error"] = "Could not analyze VIDA screenshot after navigation"
            return result

        new_state = data.get("state", "unknown")
        logger.info(f"Post-navigation state: {new_state}, notes: {data.get('notes', '')}")

        if new_state == "A" and data.get("categories"):
            categories = data["categories"]
            result["success"] = True
            result["categories"] = [
                {"name": c["name"], "relevance": c.get("relevance", ""), "reason": c.get("reason", "")}
                for c in categories
            ]
            logger.info(f"Extracted {len(categories)} categories after navigation, {result['claude_calls']} Claude calls")
        elif new_state == "B":
            # Still on parts list — "Back to catalogue" click may have missed.
            # Try one more time with a slightly different position.
            logger.warning("Still on parts list after navigation. Retrying 'Back to catalogue' at (215, 178)")
            await bridge_tools.left_click(bridge, 215, 178)
            await asyncio.sleep(2.5)

            prompt = CATALOG_EXTRACT_PROMPT.format(query=query)
            data, ss_b64 = await _vision_call(
                bridge, prompt,
                f"Extract TOP-LEVEL parts catalog categories. User is looking for: {query}"
            )
            result["claude_calls"] += 1

            categories = data.get("categories", []) if data else []
            if categories:
                result["success"] = True
                result["categories"] = [
                    {"name": c["name"], "relevance": c.get("relevance", ""), "reason": c.get("reason", "")}
                    for c in categories
                ]
                logger.info(f"Extracted {len(categories)} categories on retry, {result['claude_calls']} Claude calls")
            else:
                result["error"] = "Could not navigate back to category tree from parts list"
        else:
            result["error"] = f"Navigation did not reach category tree (state: {new_state})"

    except Exception as e:
        logger.error(f"Catalog browse failed: {e}", exc_info=True)
        result["error"] = str(e)

    return result


async def browse_category(bridge: WebSocketBridge, category_name: str, query: str) -> dict:
    """
    Click into a specific category and extract parts or subcategories.

    Returns:
        {
            "success": bool,
            "categories": [{"name": str, "relevance": str, "reason": str}],  # subcategories if any
            "parts": [{"partNumber": str, "description": str, "found": bool, "notes": str}],
            "claude_calls": int,
            "error": str,
        }
    """
    result = {"success": False, "categories": [], "parts": [], "claude_calls": 0, "error": ""}

    try:
        # Step 1: Take screenshot and locate the category
        prompt = CATEGORY_LOCATE_PROMPT.format(category_name=category_name, query=query)
        loc_data, ss_b64 = await _vision_call(
            bridge, prompt,
            f"Find category '{category_name}' in this VIDA screenshot. User is looking for: {query}"
        )
        result["claude_calls"] += 1

        if not loc_data or not loc_data.get("found"):
            result["error"] = f"Category '{category_name}' not found in the parts catalog"
            return result

        # Step 2: Click the category
        target = loc_data["target"]
        logger.info(f"Clicking category '{category_name}' at ({target['x']}, {target['y']})")
        await bridge_tools.left_click(bridge, target["x"], target["y"])
        await asyncio.sleep(1.5)

        # Step 3: Take screenshot and check for subcategories
        prompt = CATEGORY_LOCATE_PROMPT.format(category_name=category_name, query=query)
        expand_data, ss_b64 = await _vision_call(
            bridge, prompt,
            f"I just clicked category '{category_name}'. Check if it expanded to show subcategories. User looking for: {query}"
        )
        result["claude_calls"] += 1

        subcategories = expand_data.get("subcategories", []) if expand_data else []

        if subcategories:
            # Find the most relevant subcategory and auto-drill into it
            high_relevance = [s for s in subcategories if s.get("relevance") == "high"]
            best_sub = high_relevance[0] if high_relevance else subcategories[0]

            logger.info(f"Drilling into subcategory: {best_sub['name']} at ({best_sub['x']}, {best_sub['y']})")
            await bridge_tools.left_click(bridge, best_sub["x"], best_sub["y"])
            await asyncio.sleep(1.5)

            # Take screenshot of the parts list
            ss_b64 = await bridge_tools.screenshot(bridge)

            # Return the subcategories so the user knows what's available
            result["categories"] = [
                {"name": s["name"], "relevance": s.get("relevance", ""), "reason": s.get("reason", "")}
                for s in subcategories
            ]

        # Step 4: Extract parts from the current view
        ss_bytes = base64.b64decode(ss_b64)
        raw_text = await extract_results_with_claude(ss_bytes, query)
        result["claude_calls"] += 1

        parts_data = parse_agent_response(raw_text)
        result["parts"] = parts_data
        result["success"] = True

        logger.info(f"Category browse complete: {len(parts_data)} parts found, "
                     f"{len(subcategories)} subcategories, {result['claude_calls']} Claude calls")

    except Exception as e:
        logger.error(f"Category browse failed: {e}", exc_info=True)
        result["error"] = str(e)

    return result
