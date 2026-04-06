"""
VIDA application lifecycle manager.

Manages VIDA process state on the remote Windows machine via PowerShell:
- Detect if VIDA is running
- Launch VIDA if not running
- Bring VIDA window to foreground
- Ensure VIDA is ready for search (orchestrator)
- Recover to home/search screen if stuck
"""

import asyncio
import logging
from dataclasses import dataclass

from agent import tools as bridge_tools
from agent.direct_search import detect_screen, go_to_search_vehicle

logger = logging.getLogger(__name__)

# Known VIDA process names / window titles — confirm via PowerShell discovery
VIDA_PROCESS_NAMES = ["VIDA", "VIDASetup", "VIDAClient"]
VIDA_WINDOW_TITLE_PATTERN = "*VIDA*"
VIDA_EXECUTABLE_PATH = r"C:\Program Files (x86)\VIDA\VIDA.exe"

# Timeouts
LAUNCH_TIMEOUT_S = 30
LAUNCH_POLL_INTERVAL_S = 2


@dataclass
class LifecycleStatus:
    ready: bool
    screen: str = "unknown"
    launched: bool = False
    recovered: bool = False
    error: str = ""


async def is_vida_running(bridge) -> bool:
    """Check if VIDA process is running on the remote Windows machine."""
    # Use Get-Process with window title matching
    ps_command = (
        "Get-Process | Where-Object { $_.MainWindowTitle -like '"
        + VIDA_WINDOW_TITLE_PATTERN
        + "' } | Select-Object -First 1 | "
        "ForEach-Object { $_.ProcessName }"
    )
    result = await bridge_tools.powershell_exec(bridge, ps_command)

    if result["exitCode"] != 0:
        logger.warning(f"PowerShell Get-Process failed: {result['stderr']}")
        # Fallback: check by process name directly
        for name in VIDA_PROCESS_NAMES:
            fallback = await bridge_tools.powershell_exec(
                bridge, f"Get-Process -Name '{name}' -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object {{ $_.Id }}"
            )
            if fallback["exitCode"] == 0 and fallback["stdout"].strip():
                return True
        return False

    process_name = result["stdout"].strip()
    running = len(process_name) > 0
    logger.info(f"VIDA running check: {'yes' if running else 'no'} (process: {process_name or 'none'})")
    return running


async def launch_vida(bridge) -> bool:
    """Launch VIDA on the remote Windows machine and wait until it's ready.

    Returns True if VIDA was successfully launched within timeout.
    """
    logger.info(f"Launching VIDA: {VIDA_EXECUTABLE_PATH}")

    # Start the process
    ps_command = f"Start-Process '{VIDA_EXECUTABLE_PATH}' -PassThru | ForEach-Object {{ $_.Id }}"
    result = await bridge_tools.powershell_exec(bridge, ps_command)

    if result["exitCode"] != 0:
        logger.error(f"Failed to launch VIDA: {result['stderr']}")
        return False

    pid = result["stdout"].strip()
    logger.info(f"VIDA process started with PID: {pid}")

    # Poll until VIDA window appears
    elapsed = 0
    while elapsed < LAUNCH_TIMEOUT_S:
        await asyncio.sleep(LAUNCH_POLL_INTERVAL_S)
        elapsed += LAUNCH_POLL_INTERVAL_S

        if await is_vida_running(bridge):
            logger.info(f"VIDA window detected after {elapsed}s")
            # Give it a moment to fully render
            await asyncio.sleep(2)
            return True

    logger.error(f"VIDA did not become ready within {LAUNCH_TIMEOUT_S}s")
    return False


async def bring_to_foreground(bridge) -> bool:
    """Bring the VIDA window to the foreground using AppActivate."""
    ps_command = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        "$proc = Get-Process | Where-Object { $_.MainWindowTitle -like '"
        + VIDA_WINDOW_TITLE_PATTERN
        + "' } | Select-Object -First 1; "
        "if ($proc) { "
        "[Microsoft.VisualBasic.Interaction]::AppActivate($proc.Id); "
        "Write-Output 'activated' "
        "} else { Write-Output 'not_found' }"
    )
    result = await bridge_tools.powershell_exec(bridge, ps_command)

    if result["exitCode"] != 0:
        logger.warning(f"AppActivate failed: {result['stderr']}")
        return False

    output = result["stdout"].strip()
    if output == "activated":
        logger.info("VIDA window brought to foreground")
        return True

    logger.warning("VIDA window not found for foreground activation")
    return False


async def go_to_home(bridge) -> tuple[dict, int]:
    """Navigate VIDA to the Search Vehicle page (home/recovery).

    Reuses the existing detect_screen + go_to_search_vehicle flow from direct_search.
    Returns (detection_result, claude_calls).
    """
    detection, _ = await detect_screen(bridge)
    detection, nav_calls = await go_to_search_vehicle(bridge, detection)
    return detection, nav_calls + 1  # +1 for the initial detect_screen call


async def ensure_ready(bridge) -> LifecycleStatus:
    """Orchestrator: ensure VIDA is running, in foreground, and on the search page.

    Steps:
    1. Check if VIDA is running → launch if needed
    2. Bring to foreground
    3. Detect current screen → navigate to search page if needed

    Returns LifecycleStatus with details of what actions were taken.
    """
    status = LifecycleStatus(ready=False)

    try:
        # Step 1: Check if running, launch if needed
        running = await is_vida_running(bridge)
        if not running:
            logger.info("VIDA not running — launching...")
            launched = await launch_vida(bridge)
            if not launched:
                status.error = "Failed to launch VIDA within timeout"
                return status
            status.launched = True

        # Step 2: Bring to foreground
        await bring_to_foreground(bridge)
        await asyncio.sleep(0.5)

        # Step 3: Detect screen and navigate to search page
        detection, claude_calls = await go_to_home(bridge)
        screen = detection.get("screen", "unknown")
        status.screen = screen

        if screen in ("search_vehicle", "fine_tune"):
            status.ready = True
            if screen != "search_vehicle" or claude_calls > 1:
                status.recovered = True
            logger.info(f"VIDA ready on screen: {screen} (launched={status.launched}, recovered={status.recovered})")
        else:
            status.error = f"Could not navigate to search page (stuck on '{screen}')"
            logger.error(status.error)

    except Exception as e:
        logger.error(f"ensure_ready failed: {e}", exc_info=True)
        status.error = str(e)

    return status
