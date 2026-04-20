"""add search_jobs table

Revision ID: 9b5e1f7d0c22
Revises: 8a3c4d2e6b10
Create Date: 2026-04-20 16:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "9b5e1f7d0c22"
down_revision: str | None = "8a3c4d2e6b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("query_text", sa.String(length=300), nullable=False),
        sa.Column("normalized_query", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("search_history_id", sa.UUID(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("max_results", sa.Integer(), nullable=False, server_default="10"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_search_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["search_history_id"], ["search_history.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_search_jobs_status_started",
        "search_jobs",
        ["status", "started_at"],
        unique=False,
        postgresql_ops={"started_at": "DESC"},
    )
    op.create_index(
        "idx_search_jobs_normalized_running",
        "search_jobs",
        ["normalized_query"],
        unique=False,
        postgresql_where="status = 'running'",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_search_jobs_normalized_running", table_name="search_jobs"
    )
    op.drop_index("idx_search_jobs_status_started", table_name="search_jobs")
    op.drop_table("search_jobs")
