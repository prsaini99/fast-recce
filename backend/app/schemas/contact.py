"""Pydantic schemas for PropertyContact and DoNotContact."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ContactType = Literal["phone", "email", "whatsapp", "form", "website", "instagram"]
DncContactType = Literal["phone", "email", "whatsapp", "domain"]


class PropertyContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    property_id: UUID
    contact_type: ContactType
    contact_value: str
    normalized_value: str
    source_name: str
    source_url: str | None
    extraction_method: str | None
    confidence: float
    is_public_business_contact: bool
    is_verified: bool
    is_primary: bool
    flagged_personal: bool
    first_seen_at: datetime
    last_seen_at: datetime


class DoNotContactCreate(BaseModel):
    contact_type: DncContactType
    contact_value: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1)


class DoNotContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    contact_type: DncContactType
    contact_value: str
    reason: str
    created_at: datetime


class ContactResolutionResult(BaseModel):
    """Summary returned by ContactService.resolve_contacts()."""

    property_id: UUID
    contacts_in: int                # how many ExtractedContacts came in
    contacts_persisted: int         # how many rows ended up in the DB
    contacts_blocked_by_dnc: int    # how many were silently dropped
    contacts_flagged_personal: int  # how many were stored but flagged
    canonical_phone: str | None
    canonical_email: str | None
    canonical_website: str | None
