"""Pydantic schemas for the VIDA agent API."""

from pydantic import BaseModel


class SearchPartsRequest(BaseModel):
    query: str
    vin: str = ""
    model: str = ""
    year: str = ""
    max_steps: int = 50


class PartResult(BaseModel):
    partNumber: str
    description: str = ""
    found: bool = True
    quantity: int = 0
    notes: str = ""


class SearchPartsResponse(BaseModel):
    success: bool
    parts: list[PartResult] = []
    raw_response: str = ""
    steps_taken: int = 0
    error: str = ""


class HealthResponse(BaseModel):
    status: str
    piglet_connected: bool
    piglet_fingerprint: str = ""
    vida_process_running: bool = False
    search_queue_depth: int = 0
    search_busy: bool = False


class LifecycleResponse(BaseModel):
    ready: bool
    screen: str = "unknown"
    launched: bool = False
    recovered: bool = False
    error: str = ""


class BrowseCatalogRequest(BaseModel):
    query: str
    category: str = ""
    subcategory: str = ""


class CatalogCategory(BaseModel):
    name: str
    relevance: str = ""
    reason: str = ""


class BrowseCatalogResponse(BaseModel):
    success: bool
    categories: list[CatalogCategory] = []
    parts: list[PartResult] = []
    claude_calls: int = 0
    error: str = ""


class ClassifyPartRequest(BaseModel):
    query: str
    use_llm: bool = False


class PredictedPath(BaseModel):
    category: str
    subcategory: str = ""
    confidence: float = 0.0
    reasoning: str = ""


class ClassifyPartResponse(BaseModel):
    predicted_paths: list[PredictedPath] = []
    is_exact_part_number: bool = False
    part_family: str = "unknown"


class FullTreeSearchRequest(BaseModel):
    query: str
    exclude_categories: list[str] = []
    max_categories: int = 4
