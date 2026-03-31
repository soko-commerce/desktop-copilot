"""FastAPI routes for the VIDA agent service."""

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
from agent.vida_screens import VidaScreen
from bridge.ws_bridge import WebSocketBridge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vida")

# Will be set by main.py on startup
bridge: WebSocketBridge = None  # type: ignore


@router.get("/health", response_model=HealthResponse)
async def health():
    detector = get_screen_detector()
    return HealthResponse(
        status="ok" if bridge and bridge.connected else "no_piglet",
        piglet_connected=bridge.connected if bridge else False,
        piglet_fingerprint=bridge._piglet_fingerprint or "",
    )


@router.post("/search-parts", response_model=SearchPartsResponse)
async def search_parts(req: SearchPartsRequest):
    """
    Search VIDA for parts. Three-tier execution:
      1. Scripted workflow (0 Claude calls on happy path)
      2. Claude OCR on results screenshot (1 Claude call for extraction)
      3. Full AI agent fallback (10-30 Claude calls if script fails)
    """
    if not bridge or not bridge.connected:
        raise HTTPException(503, "No piglet connected — cannot control VIDA")

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
    prompt = VIDA_SEARCH_PROMPT.format(query=req.query)
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


@router.get("/calibration-status")
async def calibration_status():
    """Check which screens have been calibrated."""
    detector = get_screen_detector()
    return {
        "calibrated": detector.is_calibrated,
        "screens": list(detector._signatures.keys()),
    }
