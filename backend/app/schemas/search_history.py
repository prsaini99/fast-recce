"""Schemas for search history (sidebar + autocomplete)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SearchHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    query_text: str
    inferred_city: str | None
    inferred_property_type: str | None
    result_count: int
    search_count: int
    last_searched_at: datetime


class SearchHistorySuggestion(BaseModel):
    query_text: str
    result_count: int
    last_searched_at: datetime
