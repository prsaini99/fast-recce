"""PropertyContact and DoNotContact ORM models — M5 owns these tables."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import (
    CONTACT_TYPES,
    DNC_CONTACT_TYPES,
    check_constraint,
)


class PropertyContact(Base):
    __tablename__ = "property_contacts"
    __table_args__ = (
        CheckConstraint(
            f"contact_type {check_constraint(CONTACT_TYPES)}",
            name="ck_property_contacts_type",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_property_contacts_confidence_range",
        ),
        # Dedup contacts within a property by (type, normalized_value).
        UniqueConstraint(
            "property_id",
            "contact_type",
            "normalized_value",
            name="uq_property_contacts_property_type_value",
        ),
        Index("idx_contacts_property", "property_id"),
        # Dedup queries across properties (M6 will use these to spot shared contacts).
        Index(
            "idx_contacts_phone_dedup",
            "normalized_value",
            postgresql_where="contact_type = 'phone'",
        ),
        Index(
            "idx_contacts_email_dedup",
            "normalized_value",
            postgresql_where="contact_type = 'email'",
        ),
        Index(
            "idx_contacts_flagged",
            "flagged_personal",
            postgresql_where="flagged_personal = true",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
    )

    contact_type: Mapped[str] = mapped_column(String(20), nullable=False)
    contact_value: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(500), nullable=False)

    # Provenance — audit trail for compliance
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Quality / compliance flags
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    is_public_business_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flagged_personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DoNotContact(Base):
    __tablename__ = "do_not_contact"
    __table_args__ = (
        CheckConstraint(
            f"contact_type {check_constraint(DNC_CONTACT_TYPES)}",
            name="ck_do_not_contact_type",
        ),
        UniqueConstraint(
            "contact_type",
            "contact_value",
            name="uq_do_not_contact_type_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Stored in already-normalized form (lowercased email, digits-only phone)
    contact_value: Mapped[str] = mapped_column(String(500), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
