"""Public search schemas — used by the user-facing search flow (product pivot).

Separate from the admin dashboard schemas so we can evolve the shape of the
end-user API without breaking the review/outreach surfaces the senior
asked us to leave alone.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.query_bank import PropertyType


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Free-text query the user types. We try to infer city + property_type
    # from it if those fields aren't provided explicitly.
    query: str = Field(min_length=2, max_length=300)

    city: str | None = Field(default=None, max_length=100)
    property_type: PropertyType | None = None

    max_results: int = Field(default=10, ge=1, le=30)

    # Per-request scraper overrides. None = use the env default, True/False
    # force the source on or off regardless of env. Gives the UI a way to
    # flip individual sources on a per-search basis without an env change.
    use_airbnb: bool | None = None
    use_magicbricks: bool | None = None
    use_acres99: bool | None = None

    # When true, skip the cache lookup and run the scrape pipeline again.
    # New property IDs get unioned into the existing search_history row so
    # the UI sees a growing result set rather than a replacement. Used by
    # the "Find more results" button.
    refresh: bool = False

    # Cap on how many NEW unique properties (not already in this query's
    # history row) we want this refresh pass to return. Lets the "Find
    # more results" popover ask the user for N and guarantee up-to-N
    # actually-new rows rather than a pile of duplicates. Ignored when
    # refresh=false. None = fall back to `max_results`.
    additional_results: int | None = Field(default=None, ge=1, le=20)


class SearchSubScore(BaseModel):
    name: str
    value: float = Field(ge=0.0, le=1.0)
    weight: float
    source: Literal["deterministic", "llm", "fallback"]


class SearchResultItem(BaseModel):
    """Compact projection returned to end users. No review/outreach fields."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    city: str
    locality: str | None
    property_type: str
    relevance_score: float | None
    short_brief: str | None
    canonical_phone: str | None
    canonical_email: str | None
    canonical_website: str | None
    google_rating: float | None
    google_review_count: int | None

    # Optional UX helpers:
    sub_scores: list[SearchSubScore] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)

    # Surfaced separately so the frontend doesn't have to dig into features:
    # - `primary_image_url` is the listing's hero photo (external-source rows)
    # - `external_url` is the third-party listing URL (airbnb.com, magicbricks.com)
    # - `source_label` is the human-readable source name used in the
    #   "View on {source_label} ↗" pill ("Airbnb", "MagicBricks"); null for
    #   Google-Places / legacy rows that use `canonical_website` instead.
    primary_image_url: str | None = None
    external_url: str | None = None
    source_label: str | None = None

    # When this property was first discovered by a *different* query than the
    # one the user just ran, we echo that earlier query back so the frontend
    # can render a "first seen in <query>" link. Null when the property is
    # new to this search.
    source_query_id: UUID | None = None
    source_query_text: str | None = None


class SearchResponse(BaseModel):
    query: str
    inferred_city: str | None
    inferred_property_type: str | None
    results: list[SearchResultItem]
    candidates_discovered: int
    candidates_new: int
    candidates_skipped_known: int
    candidates_filtered_non_shoot: int = 0
    airbnb_listings_scraped: int = 0
    magicbricks_listings_scraped: int = 0
    acres99_listings_scraped: int = 0
    duration_seconds: float
    errors: list[str] = Field(default_factory=list)
