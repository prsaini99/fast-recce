"""drop users table and user refs (assigned_to, added_by)

Revision ID: 7f2a1b3c9e40
Revises: 4af2187029ca
Create Date: 2026-04-20 14:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "7f2a1b3c9e40"
down_revision: str | None = "4875a0dc2f58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # outreach_queue.assigned_to
    op.drop_index(
        "idx_outreach_assigned_status", table_name="outreach_queue"
    )
    with op.batch_alter_table("outreach_queue") as batch_op:
        batch_op.drop_constraint(
            "outreach_queue_assigned_to_fkey", type_="foreignkey"
        )
        batch_op.drop_column("assigned_to")

    # do_not_contact.added_by
    with op.batch_alter_table("do_not_contact") as batch_op:
        batch_op.drop_column("added_by")

    # users table
    op.drop_index("idx_users_email_lower", table_name="users")
    op.drop_table("users")


def downgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'reviewer', 'sales', 'viewer')",
            name="ck_users_role",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index(
        "idx_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )

    with op.batch_alter_table("do_not_contact") as batch_op:
        batch_op.add_column(sa.Column("added_by", sa.UUID(), nullable=True))

    with op.batch_alter_table("outreach_queue") as batch_op:
        batch_op.add_column(sa.Column("assigned_to", sa.UUID(), nullable=True))
        batch_op.create_foreign_key(
            "outreach_queue_assigned_to_fkey",
            "users",
            ["assigned_to"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_outreach_assigned_status",
        "outreach_queue",
        ["assigned_to", "status"],
        unique=False,
    )
