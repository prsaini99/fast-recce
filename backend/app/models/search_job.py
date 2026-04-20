"""SearchJob ORM model — one row per user search submission.

Lets long-running scrapes execute in the background so the HTTP request
returns immediately and the client can poll for status. A completed job
points at the canonical `search_history` row produced by the scrape.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Statuses the lifecycle cycles through. Using a CHECK constraint on a
# VARCHAR instead of a Postgres ENUM so we can add values in a plain
# alembic migration.
SEARCH_JOB_STATUSES = ("running", "completed", "failed")


class SearchJob(Base):
    __tablename__ = "search_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_search_jobs_status",
        ),
        Index(
            "idx_search_jobs_status_started",
            "status",
            "started_at",
            postgresql_ops={"started_at": "DESC"},
        ),
        Index(
            "idx_search_jobs_normalized_running",
            "normalized_query",
            postgresql_where="status = 'running'",
        ),
        # At most one running job per normalized query. Race-safe
        # coalescing of concurrent POST /search submissions.
        Index(
            "uq_search_jobs_running_per_query",
            "normalized_query",
            unique=True,
            postgresql_where="status = 'running'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    query_text: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_query: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")

    # Filled when the scrape succeeds — points at the canonical search_history
    # row that stores the property IDs. ON DELETE SET NULL so old jobs survive
    # a cache-history cleanup.
    search_history_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("search_history.id", ondelete="SET NULL"),
        nullable=True,
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_results: Mapped[int] = mapped_column(nullable=False, default=10)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
