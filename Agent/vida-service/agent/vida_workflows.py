"""
Scripted VIDA workflows — deterministic action sequences for known tasks.

Instead of asking Claude "what should I click?" at every step, we follow
a predefined script. Claude is only called when something unexpected happens.

Cost: 0 Claude calls on happy path (all actions are pre-defined coordinates).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agent.vida_screens import VidaScreen
from agent.screen_detector import ScreenDetector
from agent.workflow_cache import WorkflowCache, WorkflowTrace, TraceStep
from agent.screen_hash import compute_phash
from agent import tools as bridge_tools
from bridge.ws_bridge import WebSocketBridge

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    KEY = "key"
    WAIT = "wait"
    EXTRACT = "extract"


@dataclass
class WorkflowStep:
    """A single step in a scripted workflow."""
    expect_screen: VidaScreen          # Screen we expect to be on
    action: ActionType                  # What to do
    target: str                         # Element name (for click) or text (for type/key)
    next_screen: VidaScreen             # Screen we expect after the action
    delay_after: float = 0.5           # Seconds to wait after action (for UI to settle)
    allow_same_screen: bool = False     # If True, don't fail if screen doesn't change


# The primary workflow: search for a part number in VIDA
PART_SEARCH_WORKFLOW = [
    # Step 1: From home, open the hamburger menu
    WorkflowStep(
        expect_screen=VidaScreen.HOME,
        action=ActionType.CLICK,
        target="hamburger_menu",
        next_screen=VidaScreen.MENU_OPEN,
    ),
    # Step 2: Click Parts Catalog in the menu
    WorkflowStep(
        expect_screen=VidaScreen.MENU_OPEN,
        action=ActionType.CLICK,
        target="parts_catalog",
        next_screen=VidaScreen.PARTS_CATALOG,
    ),
    # Step 3: Click the search field
    WorkflowStep(
        expect_screen=VidaScreen.PARTS_CATALOG,
        action=ActionType.CLICK,
        target="search_field",
        next_screen=VidaScreen.PARTS_CATALOG,
        allow_same_screen=True,
    ),
    # Step 4: Type the part number (placeholder replaced at runtime)
    WorkflowStep(
        expect_screen=VidaScreen.PARTS_CATALOG,
        action=ActionType.TYPE,
        target="{query}",  # Replaced with actual part number
        next_screen=VidaScreen.PARTS_CATALOG,
        allow_same_screen=True,
    ),
    # Step 5: Press Enter to search
    WorkflowStep(
        expect_screen=VidaScreen.PARTS_CATALOG,
        action=ActionType.KEY,
        target="Return",
        next_screen=VidaScreen.SEARCH_RESULTS,
        delay_after=2.0,  # Search may take time
    ),
    # Step 6: Extract results from the screen
    WorkflowStep(
        expect_screen=VidaScreen.SEARCH_RESULTS,
        action=ActionType.EXTRACT,
        target="results_table",
        next_screen=VidaScreen.SEARCH_RESULTS,
        allow_same_screen=True,
    ),
]


class WorkflowExecutor:
    """Executes scripted workflows against VIDA via the bridge."""

    def __init__(self, bridge: WebSocketBridge, detector: ScreenDetector,
                 cache: Optional[WorkflowCache] = None):
        self.bridge = bridge
        self.detector = detector
        self.cache = cache

    async def execute_part_search(self, query: str) -> dict:
        """
        Execute the part search workflow.

        Returns:
            {
                "success": bool,
                "completed_steps": int,
                "failed_at_step": int | None,
                "failure_reason": str | None,
                "screenshot_bytes": bytes | None,  # final screenshot for extraction
                "used_fallback": bool,
            }
        """
        steps = PART_SEARCH_WORKFLOW
        result = {
            "success": False,
            "completed_steps": 0,
            "failed_at_step": None,
            "failure_reason": None,
            "screenshot_bytes": None,
            "used_fallback": False,
        }
        trace_steps: list[TraceStep] = []

        for i, step in enumerate(steps):
            logger.info(f"Workflow step {i+1}/{len(steps)}: {step.action.value} -> {step.target}")

            # Take screenshot and detect current screen
            screenshot = await bridge_tools.screenshot(self.bridge)
            import base64
            screenshot_bytes = base64.b64decode(screenshot) if not isinstance(screenshot, bytes) else screenshot

            current_screen = self.detector.detect_screen(screenshot_bytes)
            logger.info(f"  Current screen: {current_screen.value}, expected: {step.expect_screen.value}")

            # Compute screen hash for trace recording
            screen_hash = compute_phash(screenshot_bytes)

            # Check if we're on the expected screen
            if current_screen != step.expect_screen and current_screen != VidaScreen.UNKNOWN:
                result["failed_at_step"] = i
                result["failure_reason"] = (
                    f"Expected {step.expect_screen.value}, got {current_screen.value}"
                )
                result["screenshot_bytes"] = screenshot_bytes
                return result

            # If screen is UNKNOWN and detector is calibrated, this is unexpected
            if current_screen == VidaScreen.UNKNOWN and self.detector.is_calibrated:
                result["failed_at_step"] = i
                result["failure_reason"] = "Unknown screen state"
                result["screenshot_bytes"] = screenshot_bytes
                return result

            # Get element coordinates for trace recording
            target_x, target_y, text = None, None, None
            if step.action == ActionType.CLICK:
                elements = self.detector.get_elements(step.expect_screen)
                el = elements.get(step.target)
                if el:
                    target_x, target_y = el["x"], el["y"]
            elif step.action in (ActionType.TYPE, ActionType.KEY):
                text = step.target

            # Execute the action
            try:
                await self._execute_step(step, query)
            except Exception as e:
                result["failed_at_step"] = i
                result["failure_reason"] = f"Action failed: {str(e)}"
                return result

            # Record trace step
            trace_steps.append(TraceStep(
                screen_hash=screen_hash,
                action=step.action.value,
                target_x=target_x,
                target_y=target_y,
                text=text,
                timestamp=time.time(),
            ))

            # Wait for UI to settle
            await asyncio.sleep(step.delay_after)

            result["completed_steps"] = i + 1

            # For extract steps, capture the final screenshot
            if step.action == ActionType.EXTRACT:
                final_ss = await bridge_tools.screenshot(self.bridge)
                result["screenshot_bytes"] = (
                    base64.b64decode(final_ss) if not isinstance(final_ss, bytes) else final_ss
                )

        result["success"] = True

        # Save successful workflow trace for future replay
        if self.cache and trace_steps:
            screen_w = bridge_tools._screen_w or 1024
            screen_h = bridge_tools._screen_h or 768
            trace = WorkflowTrace(
                workflow_type="part_search",
                steps=trace_steps,
                screen_width=screen_w,
                screen_height=screen_h,
                recorded_at=time.time(),
                success=True,
            )
            self.cache.save_trace(trace)

        return result

    async def _execute_step(self, step: WorkflowStep, query: str):
        """Execute a single workflow step."""
        if step.action == ActionType.CLICK:
            # Look up element coordinates from screen definition
            elements = self.detector.get_elements(step.expect_screen)
            element = elements.get(step.target)
            if not element:
                raise ValueError(f"Unknown element '{step.target}' on {step.expect_screen.value}")
            await bridge_tools.left_click(self.bridge, element["x"], element["y"])

        elif step.action == ActionType.TYPE:
            text = step.target.replace("{query}", query)
            await bridge_tools.type_text(self.bridge, text)

        elif step.action == ActionType.KEY:
            await bridge_tools.key_press(self.bridge, step.target)

        elif step.action == ActionType.WAIT:
            await asyncio.sleep(float(step.target))

        elif step.action == ActionType.EXTRACT:
            # Extraction is handled by the caller using the screenshot
            pass
