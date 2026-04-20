"""OutreachQueue ORM model — outreach pipeline (M9).

One row per approved property. Tracks assignment, status, contact attempts.
Created automatically when a reviewer approves a property; never more than
one row per property (UNIQUE on property_id).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import (
    OUTREACH_CHANNELS,
    OUTREACH_STATUSES,
    check_constraint,
)


class OutreachQueue(Base):
    __tablename__ = "outreach_queue"
    __table_args__ = (
        CheckConstraint(
            f"status {check_constraint(OUTREACH_STATUSES)}",
            name="ck_outreach_status",
        ),
        CheckConstraint(
            f"outreach_channel IS NULL OR outreach_channel "
            f"{check_constraint(OUTREACH_CHANNELS)}",
            name="ck_outreach_channel",
        ),
        CheckConstraint(
            "priority >= 1 AND priority <= 100", name="ck_outreach_priority_range"
        ),
        UniqueConstraint("property_id", name="uq_outreach_property"),
        Index(
            "idx_outreach_priority",
            "priority",
            "created_at",
            postgresql_where="status = 'pending'",
            postgresql_ops={"priority": "DESC"},
        ),
        Index(
            "idx_outreach_follow_up",
            "follow_up_at",
            postgresql_where="follow_up_at IS NOT NULL AND status IN ('contacted', 'follow_up')",
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

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    outreach_channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    suggested_angle: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    follow_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    contact_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
