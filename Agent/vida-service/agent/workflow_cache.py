"""
Workflow-level caching — remember entire navigation sequences.

Key insight: searching "31330053" uses the exact same clicks as "31330054".
The cache key is the workflow type, not the specific query.
"""

import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
TRACES_FILE = DATA_DIR / "workflow_traces.json"


@dataclass
class TraceStep:
    """A recorded step from a successful workflow execution."""
    screen_hash: str           # Perceptual hash of screen at this step
    action: str                # "click", "type", "key"
    target_x: int | None       # Click x (model coords)
    target_y: int | None       # Click y (model coords)
    text: str | None           # Text typed or key pressed
    timestamp: float           # When this step was executed


@dataclass
class WorkflowTrace:
    """A complete recorded workflow execution."""
    workflow_type: str          # e.g., "part_search"
    steps: list[TraceStep]
    screen_width: int
    screen_height: int
    recorded_at: float
    success: bool


class WorkflowCache:
    """Persists and replays workflow traces."""

    def __init__(self):
        self._traces: dict[str, WorkflowTrace] = {}
        self._load()

    def _load(self):
        if TRACES_FILE.exists():
            try:
                with open(TRACES_FILE) as f:
                    data = json.load(f)
                for wf_type, trace_data in data.items():
                    steps = [TraceStep(**s) for s in trace_data["steps"]]
                    self._traces[wf_type] = WorkflowTrace(
                        workflow_type=trace_data["workflow_type"],
                        steps=steps,
                        screen_width=trace_data["screen_width"],
                        screen_height=trace_data["screen_height"],
                        recorded_at=trace_data["recorded_at"],
                        success=trace_data["success"],
                    )
                logger.info(f"Loaded {len(self._traces)} cached workflow traces")
            except Exception as e:
                logger.warning(f"Failed to load workflow traces: {e}")

    def _save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        for wf_type, trace in self._traces.items():
            data[wf_type] = {
                "workflow_type": trace.workflow_type,
                "steps": [asdict(s) for s in trace.steps],
                "screen_width": trace.screen_width,
                "screen_height": trace.screen_height,
                "recorded_at": trace.recorded_at,
                "success": trace.success,
            }
        with open(TRACES_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def has_trace(self, workflow_type: str) -> bool:
        trace = self._traces.get(workflow_type)
        return trace is not None and trace.success

    def get_trace(self, workflow_type: str) -> WorkflowTrace | None:
        trace = self._traces.get(workflow_type)
        if trace and trace.success:
            return trace
        return None

    def save_trace(self, trace: WorkflowTrace):
        """Save a successful workflow trace for future replay."""
        if not trace.success:
            return
        self._traces[trace.workflow_type] = trace
        self._save()
        logger.info(f"Cached workflow trace: {trace.workflow_type} ({len(trace.steps)} steps)")

    def invalidate(self, workflow_type: str):
        """Remove a cached trace (e.g., after a failed replay)."""
        if workflow_type in self._traces:
            del self._traces[workflow_type]
            self._save()
            logger.info(f"Invalidated workflow trace: {workflow_type}")
