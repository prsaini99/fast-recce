"""SearchHistory ORM model — one row per unique user search query.

Lets us (a) show a sidebar of past searches, (b) autocomplete the search
input as the user types, and (c) short-circuit identical repeat queries
by returning the already-scraped property rows instead of re-running the
full discovery pipeline.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint, func, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SearchHistory(Base):
    __tablename__ = "search_history"
    __table_args__ = (
        UniqueConstraint("normalized_query", name="uq_search_history_normalized"),
        Index(
            "idx_search_history_last_searched",
            "last_searched_at",
            postgresql_ops={"last_searched_at": "DESC"},
        ),
        # B-tree prefix index for ILIKE 'abc%' style autocomplete lookups.
        Index(
            "idx_search_history_normalized_prefix",
            "normalized_query",
            postgresql_ops={"normalized_query": "text_pattern_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    query_text: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_query: Mapped[str] = mapped_column(String(300), nullable=False)

    inferred_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    inferred_property_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    result_property_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    search_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    first_searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
