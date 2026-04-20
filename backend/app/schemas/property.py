"""Pydantic schemas for Property entities.

Minimal for now — read schema + the data we get from the
discovery + crawl pipeline. Full Create/Update CRUD schemas land with M9.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.query_bank import PropertyType

PropertyStatus = Literal[
    "new", "reviewed", "approved", "rejected", "onboarded", "do_not_contact"
]


class PropertyRead(BaseModel):
    """Full property projection, used by the dashboard."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    normalized_name: str
    normalized_address: str | None
    city: str
    locality: str | None
    state: str | None
    pincode: str | None
    lat: float | None
    lng: float | None
    property_type: PropertyType
    status: PropertyStatus

    canonical_website: str | None
    canonical_phone: str | None
    canonical_email: str | None

    short_brief: str | None
    relevance_score: float | None
    score_reason_json: dict[str, Any] | None
    features_json: dict[str, Any] = Field(default_factory=dict)

    google_place_id: str | None
    google_rating: float | None
    google_review_count: int | None

    is_duplicate: bool
    duplicate_of: UUID | None

    created_at: datetime
    updated_at: datetime


class PropertyListItem(BaseModel):
    """Compact projection for the Lead Queue list view."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    city: str
    locality: str | None
    property_type: PropertyType
    status: str
    relevance_score: float | None
    short_brief: str | None
    canonical_phone: str | None
    canonical_email: str | None
    canonical_website: str | None
    google_rating: float | None

    # Backlink to the user search that first surfaced this property so the
    # Lead Queue can render a "discovered via '<query>'" pill.
    source_query_id: UUID | None = None
    source_query_text: str | None = None


class PropertyDetail(PropertyRead):
    """Lead queue detail view — includes nested contacts and outreach."""

    contacts: list[dict[str, Any]] = Field(default_factory=list)
    outreach: dict[str, Any] | None = None


class PropertyUpsertFromCandidate(BaseModel):
    """Input the pipeline hands to PropertyService when promoting a candidate."""

    candidate_id: UUID
    canonical_name: str
    city: str
    locality: str | None = None
    state: str | None = None
    pincode: str | None = None
    lat: float | None = None
    lng: float | None = None
    property_type: PropertyType
    google_place_id: str | None = None
    google_rating: float | None = None
    google_review_count: int | None = None
    website: str | None = None
    features_json: dict[str, Any] = Field(default_factory=dict)
