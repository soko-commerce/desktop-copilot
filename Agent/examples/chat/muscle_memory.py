import os
from io import BytesIO
from typing import Optional

from muscle_mem import Check, Engine


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
        self.engine = Engine() if self.enabled else None
        self._cached_click = None

        if self.enabled:
            self._ensure_models_loaded()
            self._register_methods()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def set_connection(self, connection):
        self.connection = connection

    def left_click(self, model_x: Optional[int], model_y: Optional[int]) -> str:
        if not self.enabled or model_x is None or model_y is None or self._cached_click is None:
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            return f"Left clicked at: x={model_x if model_x is not None else 'current'}, y={model_y if model_y is not None else 'current'}"

        return self._cached_click(model_x, model_y)

    # ------------------------------------------------------------------
    # muscle_mem bindings
    # ------------------------------------------------------------------
    def _register_methods(self):
        @self.engine.method(pre_check=Check(capture=self._capture_click_region, compare=self._compare_click_region))
        def cached_click(model_x: int, model_y: int) -> str:
            screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
            self.connection.left_click(screen_x, screen_y)
            return f"Left clicked at: x={model_x}, y={model_y}"

        self._cached_click = cached_click

    # ------------------------------------------------------------------
    # Capture / compare helpers
    # ------------------------------------------------------------------
    def _capture_click_region(self, model_x: int, model_y: int):
        screen_x, screen_y = self.pig_agent.to_screen_coordinates(model_x, model_y)
        screenshot_bytes = self.connection.screenshot()
        img = self._pil_image.open(BytesIO(screenshot_bytes)).convert("RGB")

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
        return score >= self.similarity_threshold

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