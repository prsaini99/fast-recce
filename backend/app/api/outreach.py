"""Outreach router — kanban list, update, stats."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_outreach_service
from app.models.outreach import OutreachQueue
from app.models.property import Property
from app.schemas.outreach import (
    OutreachRead,
    OutreachStats,
    OutreachUpdate,
)
from app.services.outreach_service import OutreachService

router = APIRouter(prefix="/api/v1/outreach", tags=["outreach"])


@router.get("", response_model=dict[str, Any])
async def list_outreach(
    status: str | None = Query(default=None, description="Comma-separated statuses."),
    city: str | None = Query(default=None),
    min_priority: int | None = Query(default=None, ge=1, le=100),
    sort: str = Query(default="priority_desc"),
    offset: int = Query(default=0, ge=0),
    page_size: int = Query(default=50, ge=1, le=100),
    service: OutreachService = Depends(get_outreach_service),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None

    items, total = await service.list_items(
        statuses=statuses,
        city=city,
        min_priority=min_priority,
        sort=sort,
        offset=offset,
        limit=page_size,
    )

    property_ids = {item.property_id for item in items}
    prop_rows = (
        (await db.execute(select(Property).where(Property.id.in_(property_ids)))).scalars().all()
        if property_ids
        else []
    )
    props_by_id = {p.id: p for p in prop_rows}

    serialized: list[OutreachRead] = []
    for item in items:
        prop = props_by_id.get(item.property_id)
        if prop is None:
            continue
        serialized.append(
            OutreachRead.model_validate(
                {
                    "id": item.id,
                    "status": item.status,
                    "priority": item.priority,
                    "outreach_channel": item.outreach_channel,
                    "suggested_angle": item.suggested_angle,
                    "contact_attempts": item.contact_attempts,
                    "first_contact_at": item.first_contact_at,
                    "last_contact_at": item.last_contact_at,
                    "follow_up_at": item.follow_up_at,
                    "notes": item.notes,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "property": prop,
                }
            )
        )

    return {
        "data": serialized,
        "meta": {
            "total_count": total,
            "offset": offset,
            "page_size": page_size,
            "has_next": offset + len(items) < total,
        },
    }


@router.patch("/{outreach_id}", response_model=dict[str, Any])
async def update_outreach(
    outreach_id: UUID,
    data: OutreachUpdate,
    service: OutreachService = Depends(get_outreach_service),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    item = await service.update(outreach_id, data)
    prop = await db.get(Property, item.property_id)
    return OutreachRead.model_validate(
        {
            "id": item.id,
            "status": item.status,
            "priority": item.priority,
            "outreach_channel": item.outreach_channel,
            "suggested_angle": item.suggested_angle,
            "contact_attempts": item.contact_attempts,
            "first_contact_at": item.first_contact_at,
            "last_contact_at": item.last_contact_at,
            "follow_up_at": item.follow_up_at,
            "notes": item.notes,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "property": prop,
        }
    ).model_dump()


@router.get("/stats", response_model=OutreachStats)
async def outreach_stats(
    city: str | None = Query(default=None),
    service: OutreachService = Depends(get_outreach_service),
) -> OutreachStats:
    return await service.stats(city=city)
