"""Pydantic schemas for the VIDA agent API."""

from pydantic import BaseModel


class SearchPartsRequest(BaseModel):
    query: str
    vin: str = ""
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
