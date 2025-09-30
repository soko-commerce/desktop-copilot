import logging
import os
from io import BytesIO
from typing import Optional

from muscle_mem import Check, Engine

# Configure logging to show muscle_mem logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Also set the muscle_mem logger to INFO level to ensure its logs are visible
muscle_mem_logger = logging.getLogger('muscle_mem')
muscle_mem_logger.setLevel(logging.INFO)

# Global engine instance
engine = Engine()


class MuscleMemoryController:
    """Wraps PigAgent tool invocations with muscle_mem caching.

    This controller captures a local screenshot crop around requested coordinates,
    embeds it with CLIP, and lets muscle_mem decide if we can reuse a previous
    trajectory instead of replaying the same action.
    """

    _clip_model = None
    _clip_processor = None
    _torch = None
    _pil_image = None

    def __init__(self, pig_agent, enabled: Optional[bool] = None, region: int = 120, similarity_threshold: float = 0.80):
        self.pig_agent = pig_agent
        self.region = region
        self.similarity_threshold = similarity_threshold
        if enabled is None:
            enabled = os.getenv("MUSCLE_MEM_ENABLED", "0") == "1"
        self.enabled = enabled
        self.connection = None
        self._finalized_engine = None

        logger.info("[muscle_mem] MuscleMemoryController initialized - enabled: %s", self.enabled)
        if not self.enabled:
            logger.info("[muscle_mem] To enable muscle memory, set MUSCLE_MEM_ENABLED=1 environment variable")

        if self.enabled:
            logger.info("[muscle_mem] Loading CLIP models and setting up caching...")
            self._ensure_models_loaded()
            self._setup_engine()
            logger.info("[muscle_mem] Muscle memory setup complete")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def set_connection(self, connection):
        self.connection = connection

    def left_click(self, model_x: Optional[int], model_y: Optional[int]) -> str:
        if not self.enabled:
            logger.debug("[muscle_mem] Muscle memory disabled, performing direct click")
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            return f"Left clicked at: x={model_x if model_x is not None else 'current'}, y={model_y if model_y is not None else 'current'}"
        
        if model_x is None or model_y is None:
            logger.debug("[muscle_mem] Coordinates are None, performing direct click")
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            return f"Left clicked at: x={model_x if model_x is not None else 'current'}, y={model_y if model_y is not None else 'current'}"
        
        if self._finalized_engine is None:
            logger.debug("[muscle_mem] Engine not finalized, performing direct click")
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            return f"Left clicked at: x={model_x if model_x is not None else 'current'}, y={model_y if model_y is not None else 'current'}"

        logger.debug("[muscle_mem] Using muscle memory for click at (%s, %s)", model_x, model_y)
        
        # Use the finalized engine to execute the click with caching
        try:
            # The finalized engine returns True for cache hit, False for cache miss
            cache_hit = self._finalized_engine(model_x, model_y)
            if cache_hit:
                logger.info("[muscle_mem] Cache hit - skipping actual click at (%s, %s)", model_x, model_y)
                self._replay_cached_click(model_x, model_y)  # Replay the cached click action
                return f"Left clicked at: x={model_x}, y={model_y} (cached)"
            else:
                logger.info("[muscle_mem] Cache miss - trajectory executed and cached at (%s, %s)", model_x, model_y)
                return f"Left clicked at: x={model_x}, y={model_y}"
        except Exception as e:
            logger.error("[muscle_mem] Error in cached click: %s", e)
            # Fall back to direct click
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            return f"Left clicked at: x={model_x}, y={model_y} (fallback)"

    # ------------------------------------------------------------------
    # muscle_mem engine setup
    # ------------------------------------------------------------------
    def _setup_engine(self):
        """Set up the muscle_mem engine following the GitHub examples pattern."""
        
        # Store a reference to self for the nested functions
        controller = self
        
        # Define the click method with muscle_mem caching
        @engine.function(pre_check=Check(controller._capture_click_region, controller._compare_click_region))
        def cached_click(model_x: int, model_y: int) -> str:
            screen_x, screen_y = controller.pig_agent.to_screen_coordinates(model_x, model_y)
            controller.connection.left_click(screen_x, screen_y)
            logger.info("[muscle_mem] Executing click at model coords (%s, %s)", model_x, model_y)
            return f"Left clicked at: x={model_x}, y={model_y}"
        
        # Create a simple agent function that performs clicks
        def agent_function(model_x: int, model_y: int):
            return cached_click(model_x, model_y)
        
        # Finalize the engine with the agent and context
        try:
            self._finalized_engine = engine.set_agent(agent_function).set_context(self).finalize()
            logger.info("[muscle_mem] Engine finalized successfully")
        except Exception as e:
            logger.error("[muscle_mem] Failed to finalize engine: %s", e)
            self._finalized_engine = None

    def _replay_cached_click(self, model_x: int, model_y: int):
        """Replays a cached click action."""
        logger.debug(f"[muscle_mem] Replaying cached click at ({model_x}, {model_y})")
        try:
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            logger.info(f"[muscle_mem] Successfully replayed cached click at ({model_x}, {model_y})")
        except Exception as e:
            logger.error(f"[muscle_mem] Error replaying cached click: {e}")

    # ------------------------------------------------------------------
    # Capture / compare helpers
    # ------------------------------------------------------------------
    def _capture_click_region(self, model_x: int, model_y: int):
        screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
        screenshot_bytes = self.connection.screenshot()
        # Fix: Access the PIL Image class correctly
        from PIL import Image
        img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")

        left = max(0, screen_x - self.region)
        top = max(0, screen_y - self.region)
        right = min(img.width, screen_x + self.region)
        bottom = min(img.height, screen_y + self.region)
        crop = img.crop((left, top, right, bottom))

        inputs = self._clip_processor(images=crop, return_tensors="pt")
        with self._torch.no_grad():
            image_features = self._clip_model.get_image_features(**inputs)
        return image_features / image_features.norm(dim=1, keepdim=True)

    def _compare_click_region(self, current, candidate) -> bool:
        score = self._torch.nn.functional.cosine_similarity(current, candidate, dim=1).item()
        is_similar = score >= self.similarity_threshold
        if is_similar:
            logger.info("[muscle_mem] Cache hit with similarity %.3f", score)
        else:
            logger.debug("[muscle_mem] Cache miss with similarity %.3f (threshold: %.3f)", score, self.similarity_threshold)
        return is_similar

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------
    def _ensure_models_loaded(self):
        if MuscleMemoryController._clip_model is not None:
            return

        from transformers import CLIPModel, CLIPProcessor  # Imported lazily
        import torch
        from PIL import Image

        model_name = os.getenv("MUSCLE_MEM_CLIP_MODEL", "openai/clip-vit-large-patch14")
        MuscleMemoryController._clip_model = CLIPModel.from_pretrained(model_name)
        MuscleMemoryController._clip_processor = CLIPProcessor.from_pretrained(model_name)
        MuscleMemoryController._torch = torch
        MuscleMemoryController._pil_image = Image

        MuscleMemoryController._clip_model.eval()
        MuscleMemoryController._clip_model.to(torch.device("cpu"))  # Allow CPU by default

    # Convenience properties
    @property
    def _clip_model(self):
        return MuscleMemoryController._clip_model

    @property
    def _clip_processor(self):
        return MuscleMemoryController._clip_processor

    @property
    def _torch(self):
        return MuscleMemoryController._torch

    @property
    def _pil_image(self):
        return MuscleMemoryController._pil_image