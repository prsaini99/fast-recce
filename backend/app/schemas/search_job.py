"""Schemas for search jobs (async scrape queue)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.search import SearchResponse

SearchJobStatus = Literal["running", "completed", "failed"]


class SearchJobRead(BaseModel):
    """Base job record — used for the sidebar list."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    query_text: str
    status: SearchJobStatus
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class SearchJobDetail(SearchJobRead):
    """Job + reconstructed SearchResponse when completed.

    Clients poll this endpoint; on `status = completed` the `response`
    field holds the full result payload (identical shape to POST /search's
    former synchronous response) and rendering can proceed.
    """

    response: SearchResponse | None = None
