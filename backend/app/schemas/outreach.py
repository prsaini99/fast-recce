"""Pydantic schemas for outreach (M9)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

OutreachStatus = Literal[
    "pending", "contacted", "responded", "follow_up",
    "converted", "declined", "no_response",
]
OutreachChannel = Literal["phone", "email", "whatsapp", "form", "in_person"]


class OutreachPropertyRef(BaseModel):
    """Compact property projection embedded in outreach list responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    canonical_name: str
    city: str
    property_type: str
    relevance_score: float | None
    canonical_phone: str | None
    canonical_email: str | None


class OutreachRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: OutreachStatus
    priority: int
    outreach_channel: OutreachChannel | None
    suggested_angle: str | None
    contact_attempts: int
    first_contact_at: datetime | None
    last_contact_at: datetime | None
    follow_up_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    property: OutreachPropertyRef


class OutreachUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OutreachStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=100)
    outreach_channel: OutreachChannel | None = None
    follow_up_at: datetime | None = None
    notes: str | None = None


class OutreachStats(BaseModel):
    total: int
    by_status: dict[str, int]
    conversion_rate: float
    avg_contact_attempts: float
