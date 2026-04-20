"""partial unique index on search_jobs for race-safe coalescing

At most one running job per normalized query. Two concurrent POST /search
submissions for the same text used to both pass the SELECT check and both
INSERT; this index makes the second INSERT fail so we can catch it and
hand back the first job's id instead of double-scraping.

Revision ID: a2c1d8f6e433
Revises: 9b5e1f7d0c22
Create Date: 2026-04-20 16:30:00.000000

"""
from collections.abc import Sequence

from alembic import op


revision: str = "a2c1d8f6e433"
down_revision: str | None = "9b5e1f7d0c22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_search_jobs_running_per_query",
        "search_jobs",
        ["normalized_query"],
        unique=True,
        postgresql_where="status = 'running'",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_search_jobs_running_per_query", table_name="search_jobs"
    )
