"""add search_history table

Revision ID: 8a3c4d2e6b10
Revises: 7f2a1b3c9e40
Create Date: 2026-04-20 15:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8a3c4d2e6b10"
down_revision: str | None = "7f2a1b3c9e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("query_text", sa.String(length=300), nullable=False),
        sa.Column("normalized_query", sa.String(length=300), nullable=False),
        sa.Column("inferred_city", sa.String(length=100), nullable=True),
        sa.Column("inferred_property_type", sa.String(length=50), nullable=True),
        sa.Column(
            "result_property_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("search_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column(
            "first_searched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_searched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_query", name="uq_search_history_normalized"
        ),
    )
    op.create_index(
        "idx_search_history_last_searched",
        "search_history",
        ["last_searched_at"],
        unique=False,
        postgresql_ops={"last_searched_at": "DESC"},
    )
    op.create_index(
        "idx_search_history_normalized_prefix",
        "search_history",
        ["normalized_query"],
        unique=False,
        postgresql_ops={"normalized_query": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index(
        "idx_search_history_normalized_prefix", table_name="search_history"
    )
    op.drop_index(
        "idx_search_history_last_searched", table_name="search_history"
    )
    op.drop_table("search_history")
