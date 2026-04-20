"""Properties router — Lead Queue list, detail, review actions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    get_briefing_service,
    get_property_service,
    get_scoring_service,
    get_search_history_service,
)
from app.schemas.property import PropertyDetail, PropertyListItem, PropertyRead
from app.schemas.review import ReviewRequest, ReviewResponse
from app.services.briefing_service import BriefingService
from app.services.property_service import PropertyService
from app.services.scoring_service import ScoringService
from app.services.search_history_service import SearchHistoryService

router = APIRouter(prefix="/api/v1/properties", tags=["properties"])


@router.get("", response_model=dict[str, Any])
async def list_properties(
    city: str | None = Query(default=None),
    property_type: str | None = Query(
        default=None,
        description="Comma-separated list of property types.",
    ),
    status: str | None = Query(
        default=None,
        description="Comma-separated list of statuses. Default: all statuses.",
    ),
    min_score: float | None = Query(default=None, ge=0, le=1),
    max_score: float | None = Query(default=None, ge=0, le=1),
    has_phone: bool | None = Query(default=None),
    has_email: bool | None = Query(default=None),
    is_duplicate: bool = Query(default=False),
    search: str | None = Query(default=None),
    synced_only: bool = Query(
        default=True,
        description="Only include properties surfaced by at least one user search.",
    ),
    sort: str = Query(default="relevance_score_desc"),
    offset: int = Query(default=0, ge=0),
    page_size: int = Query(default=50, ge=1, le=100),
    service: PropertyService = Depends(get_property_service),
    history: SearchHistoryService = Depends(get_search_history_service),
) -> dict[str, Any]:
    property_types = _split_csv(property_type)
    statuses = _split_csv(status) if status else None

    only_ids: list[UUID] | None = None
    if synced_only:
        only_ids = sorted(await history.owned_property_ids())

    items, total = await service.list_for_dashboard(
        city=city,
        property_types=property_types,
        statuses=statuses,
        min_score=min_score,
        max_score=max_score,
        has_phone=has_phone,
        has_email=has_email,
        include_duplicates=is_duplicate,
        search=search,
        only_ids=only_ids,
        sort=sort,
        offset=offset,
        limit=page_size,
    )

    # Annotate each row with the user search that first surfaced it, so the
    # Lead Queue can render a "discovered via '<query>'" backlink.
    first_sources = await history.first_search_by_property(
        [p.id for p in items]
    )
    data: list[PropertyListItem] = []
    for prop in items:
        item = PropertyListItem.model_validate(prop)
        src = first_sources.get(prop.id)
        if src is not None:
            item.source_query_id = src.id
            item.source_query_text = src.query_text
        data.append(item)

    return {
        "data": data,
        "meta": {
            "total_count": total,
            "offset": offset,
            "page_size": page_size,
            "has_next": offset + len(items) < total,
        },
    }


@router.get("/{property_id}", response_model=PropertyDetail)
async def get_property(
    property_id: UUID,
    service: PropertyService = Depends(get_property_service),
) -> PropertyDetail:
    prop, contacts, outreach = await service.get_detail(property_id)

    base = PropertyRead.model_validate(prop).model_dump()
    return PropertyDetail(
        **base,
        contacts=[
            {
                "id": str(c.id),
                "contact_type": c.contact_type,
                "contact_value": c.contact_value,
                "normalized_value": c.normalized_value,
                "source_name": c.source_name,
                "source_url": c.source_url,
                "extraction_method": c.extraction_method,
                "confidence": c.confidence,
                "is_public_business_contact": c.is_public_business_contact,
                "is_primary": c.is_primary,
                "flagged_personal": c.flagged_personal,
            }
            for c in contacts
        ],
        outreach=(
            {
                "id": str(outreach.id),
                "status": outreach.status,
                "priority": outreach.priority,
                "outreach_channel": outreach.outreach_channel,
                "contact_attempts": outreach.contact_attempts,
                "notes": outreach.notes,
            }
            if outreach is not None
            else None
        ),
    )


@router.patch("/{property_id}/review", response_model=ReviewResponse)
async def review_property(
    property_id: UUID,
    data: ReviewRequest,
    service: PropertyService = Depends(get_property_service),
) -> ReviewResponse:
    return await service.review(property_id, data)


@router.post("/{property_id}/score", response_model=PropertyDetail)
async def score_property_endpoint(
    property_id: UUID,
    scoring: ScoringService = Depends(get_scoring_service),
    service: PropertyService = Depends(get_property_service),
) -> PropertyDetail:
    """On-demand LLM scoring for a single property."""
    await scoring.score_property(property_id)
    return await _load_detail(property_id, service)


@router.post("/{property_id}/brief", response_model=PropertyDetail)
async def brief_property_endpoint(
    property_id: UUID,
    briefing: BriefingService = Depends(get_briefing_service),
    service: PropertyService = Depends(get_property_service),
) -> PropertyDetail:
    """On-demand LLM brief generation for a single property."""
    await briefing.generate_brief(property_id)
    return await _load_detail(property_id, service)


@router.post("/{property_id}/enrich", response_model=PropertyDetail)
async def enrich_property_endpoint(
    property_id: UUID,
    scoring: ScoringService = Depends(get_scoring_service),
    briefing: BriefingService = Depends(get_briefing_service),
    service: PropertyService = Depends(get_property_service),
) -> PropertyDetail:
    """Run scoring + brief in sequence. The one-click shortcut for users
    who want both at once from a result card or detail page.
    """
    await scoring.score_property(property_id)
    try:
        await briefing.generate_brief(property_id)
    except Exception:  # noqa: BLE001 — brief failure is non-fatal
        pass
    return await _load_detail(property_id, service)


async def _load_detail(
    property_id: UUID, service: PropertyService
) -> PropertyDetail:
    prop, contacts, outreach = await service.get_detail(property_id)
    base = PropertyRead.model_validate(prop).model_dump()
    return PropertyDetail(
        **base,
        contacts=[
            {
                "id": str(c.id),
                "contact_type": c.contact_type,
                "contact_value": c.contact_value,
                "normalized_value": c.normalized_value,
                "source_name": c.source_name,
                "source_url": c.source_url,
                "extraction_method": c.extraction_method,
                "confidence": c.confidence,
                "is_public_business_contact": c.is_public_business_contact,
                "is_primary": c.is_primary,
                "flagged_personal": c.flagged_personal,
            }
            for c in contacts
        ],
        outreach=(
            {
                "id": str(outreach.id),
                "status": outreach.status,
                "priority": outreach.priority,
                "outreach_channel": outreach.outreach_channel,
                "contact_attempts": outreach.contact_attempts,
                "notes": outreach.notes,
            }
            if outreach is not None
            else None
        ),
    )


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]
