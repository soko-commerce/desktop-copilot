"""FastAPI routes for the VIDA agent service."""

import asyncio
import base64
import logging
import time

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from api.models import (
    SearchPartsRequest,
    SearchPartsResponse,
    PartResult,
    HealthResponse,
    LifecycleResponse,
    BrowseCatalogRequest,
    BrowseCatalogResponse,
    CatalogCategory,
)
from agent.vida_agent import (
    build_vida_agent,
    run_scripted_search,
    extract_results_with_claude,
    get_screen_detector,
    get_workflow_cache,
)
from agent.vida_prompts import VIDA_SEARCH_PROMPT
from agent.result_parser import parse_agent_response
from agent.direct_search import execute_part_search as direct_part_search
from agent.vida_screens import VidaScreen
from agent.vida_lifecycle import is_vida_running, ensure_ready
from bridge.ws_bridge import WebSocketBridge
from bridge.direct_client import DirectPigletClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vida")

# Will be set by main.py on startup — either WebSocketBridge or DirectPigletClient
bridge: WebSocketBridge | DirectPigletClient = None  # type: ignore

# ---------- VIDA Search Queue ----------
# VIDA is a single desktop application — only one search can run at a time.
# All search endpoints acquire this lock, creating a FIFO queue.
# Requests wait in order; no search can corrupt another.
_vida_search_lock = asyncio.Lock()
_queue_depth = 0  # track how many requests are waiting


@router.get("/health", response_model=HealthResponse)
async def health():
    detector = get_screen_detector()
    vida_running = False
    if bridge and bridge.connected:
        try:
            vida_running = await is_vida_running(bridge)
        except Exception as e:
            logger.warning(f"VIDA process check failed during health: {e}")
    return HealthResponse(
        status="ok" if bridge and bridge.connected else "no_piglet",
        piglet_connected=bridge.connected if bridge else False,
        piglet_fingerprint=bridge._piglet_fingerprint or "",
        vida_process_running=vida_running,
        search_queue_depth=_queue_depth,
        search_busy=_vida_search_lock.locked(),
    )


@router.post("/search-parts", response_model=SearchPartsResponse)
async def search_parts(req: SearchPartsRequest):
    """
    Search VIDA for parts. Three-tier execution:
      1. Scripted workflow (0 Claude calls on happy path)
      2. Claude OCR on results screenshot (1 Claude call for extraction)
      3. Full AI agent fallback (10-30 Claude calls if script fails)

    Requests are serialized via FIFO queue — VIDA is a single desktop app.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected — cannot control VIDA")

    global _queue_depth
    _queue_depth += 1
    position = _queue_depth
    if position > 1:
        logger.info(f"Search queued (position {position}): {req.query}")

    async with _vida_search_lock:
        _queue_depth -= 1
        if position > 1:
            logger.info(f"Search dequeued, starting: {req.query}")
        return await _execute_search_parts(req)


async def _execute_search_parts(req: SearchPartsRequest) -> SearchPartsResponse:
    """Inner search logic — called under the VIDA search lock."""
    start = time.time()
    detector = get_screen_detector()

    # --- Tier 1: Try scripted workflow ---
    if detector.is_calibrated:
        logger.info(f"Attempting scripted workflow for: {req.query}")
        wf_result = await run_scripted_search(bridge, req.query)

        if wf_result["success"] and wf_result.get("screenshot_bytes"):
            # --- Tier 2: Claude OCR on results screenshot (1 call) ---
            logger.info("Scripted workflow succeeded — extracting results with Claude OCR")
            try:
                raw_text = await extract_results_with_claude(
                    wf_result["screenshot_bytes"], req.query
                )
                parts_data = parse_agent_response(raw_text)
                parts = [PartResult(**p) for p in parts_data]

                elapsed = time.time() - start
                logger.info(
                    f"Search completed in {elapsed:.1f}s — "
                    f"scripted ({wf_result['completed_steps']} steps) + 1 Claude OCR call"
                )

                return SearchPartsResponse(
                    success=True,
                    parts=parts,
                    raw_response=raw_text,
                    steps_taken=wf_result["completed_steps"],
                )
            except Exception as e:
                logger.warning(f"Claude OCR extraction failed: {e}, falling back to full agent")

        elif not wf_result["success"]:
            logger.info(
                f"Scripted workflow failed at step {wf_result.get('failed_at_step')}: "
                f"{wf_result.get('failure_reason')} — falling back to AI agent"
            )

    else:
        logger.info("Screen detector not calibrated — using full AI agent")

    # --- Tier 3: Full AI agent fallback ---
    logger.info(f"Running full AI agent for: {req.query}")
    agent = build_vida_agent(bridge)
    vin_line = f"VIN: {req.vin}" if req.vin else ""
    prompt = VIDA_SEARCH_PROMPT.format(query=req.query, vin_line=vin_line)
    input_messages = {"messages": [HumanMessage(content=prompt)]}
    config = {"recursion_limit": req.max_steps}

    steps = 0
    final_text = ""

    try:
        async for event in agent.astream(input_messages, config=config):
            steps += 1
            for node_name, node_output in event.items():
                if node_name == "model":
                    msgs = node_output.get("messages", [])
                    if msgs:
                        last = msgs[-1]
                        if hasattr(last, "content") and isinstance(last.content, str):
                            final_text = last.content

        elapsed = time.time() - start
        logger.info(f"AI agent search completed in {elapsed:.1f}s, {steps} steps")

        parts_data = parse_agent_response(final_text)
        parts = [PartResult(**p) for p in parts_data]

        return SearchPartsResponse(
            success=True,
            parts=parts,
            raw_response=final_text,
            steps_taken=steps,
        )

    except Exception as e:
        logger.error(f"VIDA search failed: {e}", exc_info=True)
        return SearchPartsResponse(
            success=False,
            error=str(e),
            raw_response=final_text,
            steps_taken=steps,
        )


@router.post("/search-direct", response_model=SearchPartsResponse)
async def search_direct(req: SearchPartsRequest):
    """
    Direct VIDA search — 2-3 Claude vision calls, no agent loop.
    Uses Claude to detect screen state and element positions, then
    executes a deterministic click/type sequence.

    Requests are serialized via FIFO queue — VIDA is a single desktop app.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected — cannot control VIDA")

    global _queue_depth
    _queue_depth += 1
    position = _queue_depth
    if position > 1:
        logger.info(f"Direct search queued (position {position}): {req.query}")

    async with _vida_search_lock:
        _queue_depth -= 1
        return await _execute_search_direct(req)


async def _execute_search_direct(req: SearchPartsRequest) -> SearchPartsResponse:
    """Inner direct search logic — called under the VIDA search lock."""
    start = time.time()
    logger.info(f"Direct search: query='{req.query}', vin='{req.vin}', model='{req.model}', year='{req.year}'")

    result = await direct_part_search(bridge, req.query, req.vin, req.model, req.year)

    elapsed = time.time() - start
    logger.info(f"Direct search completed in {elapsed:.1f}s — "
                f"{result['claude_calls']} Claude calls, {result['steps_taken']} steps")

    parts = [PartResult(**p) for p in result.get("parts", [])]

    return SearchPartsResponse(
        success=result["success"],
        parts=parts,
        raw_response=result.get("raw_response", ""),
        steps_taken=result.get("steps_taken", 0),
        error=result.get("error", ""),
    )


# --- Catalog browsing endpoints ---


@router.post("/browse-catalog", response_model=BrowseCatalogResponse)
async def browse_catalog(req: BrowseCatalogRequest):
    """
    Browse VIDA parts catalog visually when text search fails.

    Two modes:
      1. No category → returns ranked list of catalog categories
      2. Category specified → drills in and returns parts/subcategories

    Serialized via FIFO queue — VIDA is a single desktop app.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected — cannot browse VIDA catalog")

    global _queue_depth
    _queue_depth += 1
    position = _queue_depth
    if position > 1:
        logger.info(f"Browse queued (position {position}): {req.query}")

    async with _vida_search_lock:
        _queue_depth -= 1
        return await _execute_browse_catalog(req)


async def _execute_browse_catalog(req: BrowseCatalogRequest) -> BrowseCatalogResponse:
    """Inner browse logic — called under the VIDA search lock."""
    from agent.catalog_browse import get_catalog_categories, browse_category

    start = time.time()

    if not req.category:
        # Phase 1: return ranked categories
        logger.info(f"Browsing catalog categories for: {req.query}")
        result = await get_catalog_categories(bridge, req.query)
    else:
        # Phase 2: drill into category, return parts
        logger.info(f"Drilling into category '{req.category}' for: {req.query}")
        result = await browse_category(bridge, req.category, req.query)

    elapsed = time.time() - start
    logger.info(f"Catalog browse completed in {elapsed:.1f}s — {result.get('claude_calls', 0)} Claude calls")

    categories = [CatalogCategory(**c) for c in result.get("categories", [])]
    parts = [PartResult(**p) for p in result.get("parts", [])]

    return BrowseCatalogResponse(
        success=result.get("success", False),
        categories=categories,
        parts=parts,
        claude_calls=result.get("claude_calls", 0),
        error=result.get("error", ""),
    )


# --- Lifecycle endpoints ---


@router.get("/lifecycle", response_model=LifecycleResponse)
async def lifecycle_check():
    """Ensure VIDA is running, in foreground, and on the search page.

    Orchestrates: process check → launch if needed → foreground → navigate to search.
    Serialized with search operations — cannot run while a search is in progress.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected — cannot check VIDA lifecycle")

    async with _vida_search_lock:
        status = await ensure_ready(bridge)
    return LifecycleResponse(
        ready=status.ready,
        screen=status.screen,
        launched=status.launched,
        recovered=status.recovered,
        error=status.error,
    )


@router.post("/lifecycle/recover", response_model=LifecycleResponse)
async def lifecycle_recover():
    """Force recovery to the VIDA search/home screen.

    Use this when VIDA is stuck on an unexpected screen.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected — cannot recover VIDA")

    from agent.vida_lifecycle import go_to_home, bring_to_foreground

    try:
        await bring_to_foreground(bridge)
        detection, claude_calls = await go_to_home(bridge)
        screen = detection.get("screen", "unknown")
        ready = screen in ("search_vehicle", "fine_tune")
        return LifecycleResponse(
            ready=ready,
            screen=screen,
            recovered=True,
            error="" if ready else f"Recovery incomplete — stuck on '{screen}'",
        )
    except Exception as e:
        logger.error(f"Lifecycle recovery failed: {e}", exc_info=True)
        return LifecycleResponse(
            ready=False,
            error=str(e),
        )


# --- Calibration endpoints ---


class CalibrateRequest(BaseModel):
    screen_name: str  # VidaScreen enum value


@router.post("/calibrate")
async def calibrate_screen(req: CalibrateRequest):
    """
    Calibrate a screen by taking a screenshot and computing reference hashes.
    Navigate VIDA to the target screen first, then call this endpoint.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected")

    try:
        screen = VidaScreen(req.screen_name)
    except ValueError:
        valid = [s.value for s in VidaScreen if s != VidaScreen.UNKNOWN]
        raise HTTPException(400, f"Invalid screen name. Valid: {valid}")

    from agent import tools as bridge_tools
    screenshot_b64 = await bridge_tools.screenshot(bridge)
    screenshot_bytes = base64.b64decode(screenshot_b64)

    detector = get_screen_detector()
    detector.calibrate_screen(screen, screenshot_bytes)

    return {
        "status": "calibrated",
        "screen": screen.value,
        "message": f"Screen '{screen.value}' calibrated successfully",
    }


@router.get("/screenshot")
async def take_screenshot():
    """Take a screenshot and return as base64 PNG."""
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected")
    from agent import tools as bridge_tools
    dims = await bridge_tools.get_dimensions(bridge)
    b64 = await bridge_tools.screenshot(bridge)
    return {"image": b64, "dimensions": dims}


class ActionRequest(BaseModel):
    action: str  # click, type, key, double_click
    x: int = 0
    y: int = 0
    text: str = ""
    raw: bool = False  # if True, bypass coordinate scaling


@router.post("/action")
async def perform_action(req: ActionRequest):
    """Execute a single action via piglet — zero AI calls."""
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected")
    from agent import tools as bridge_tools
    import json
    await bridge_tools.get_dimensions(bridge)

    if req.action == "click":
        if req.raw:
            # Send raw pixel coordinates directly to piglet, bypass _to_screen
            down = json.dumps({"button": "left", "x": req.x, "y": req.y, "down": True}).encode()
            up = json.dumps({"button": "left", "x": req.x, "y": req.y, "down": False}).encode()
            await bridge.send_request("POST", "computer/input/mouse/click", body=down)
            status, _, _ = await bridge.send_request("POST", "computer/input/mouse/click", body=up)
            result = "ok" if status == 200 else f"Error: status {status}"
        else:
            result = await bridge_tools.left_click(bridge, req.x, req.y)
    elif req.action == "double_click":
        result = await bridge_tools.double_click(bridge, req.x, req.y)
    elif req.action == "right_click":
        result = await bridge_tools.right_click(bridge, req.x, req.y)
    elif req.action == "type":
        result = await bridge_tools.type_text(bridge, req.text)
    elif req.action == "key":
        result = await bridge_tools.key_press(bridge, req.text)
    else:
        raise HTTPException(400, f"Unknown action: {req.action}")
    return {"result": result}


@router.post("/calibrate-coords")
async def calibrate_coordinates():
    """
    Calibrate coordinate mapping using Windows system metrics.
    Queries SM_CXSCREEN/SM_CYSCREEN via PowerShell to determine the actual
    SendInput target space, then computes scale factors.
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected")
    from agent import tools as bridge_tools
    from agent.calibration import get_calibrator

    await bridge_tools.get_dimensions(bridge)

    calibrator = get_calibrator()
    gw = bridge_tools._screen_w or 0
    gh = bridge_tools._screen_h or 0
    fingerprint = getattr(bridge, '_piglet_fingerprint', '') or ''

    # Force re-calibration
    calibrator.invalidate()
    result = await calibrator.calibrate(bridge, gw, gh, fingerprint)

    return {
        "calibrated": True,
        "gdigrab_dimensions": {"w": result.gdigrab_w, "h": result.gdigrab_h},
        "input_dimensions": {"w": result.input_w, "h": result.input_h},
        "virtual_dimensions": {"w": result.virtual_w, "h": result.virtual_h},
        "dpi_scale": result.dpi_scale,
        "scale_factors": {"x": round(result.scale_x, 4), "y": round(result.scale_y, 4)},
        "calibrated_at": result.calibrated_at,
    }


@router.get("/calibration-status")
async def calibration_status():
    """Check coordinate calibration state and screen detection status."""
    from agent.calibration import get_calibrator

    detector = get_screen_detector()
    calibrator = get_calibrator()

    status = {
        "screen_detection": {
            "calibrated": detector.is_calibrated,
            "screens": list(detector._signatures.keys()),
        },
        "coordinate_calibration": {
            "calibrated": calibrator.is_calibrated,
        },
    }

    if calibrator.result:
        r = calibrator.result
        status["coordinate_calibration"].update({
            "gdigrab_dimensions": {"w": r.gdigrab_w, "h": r.gdigrab_h},
            "input_dimensions": {"w": r.input_w, "h": r.input_h},
            "dpi_scale": r.dpi_scale,
            "scale_factors": {"x": round(r.scale_x, 4), "y": round(r.scale_y, 4)},
            "calibrated_at": r.calibrated_at,
        })

    return status
