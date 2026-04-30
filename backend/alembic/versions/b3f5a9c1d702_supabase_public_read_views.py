"""supabase public-read RLS, dashboard views, analytics RPC

Revision ID: b3f5a9c1d702
Revises: a2c1d8f6e433
Create Date: 2026-04-30

Lets the frontend bypass the (cold-starting) Render backend for read-only
listings by querying Supabase PostgREST directly:

  - Enables row-level security on read-exposed tables and grants
    SELECT-only to the `anon` role. Mutations (write/update/delete)
    have no policy → forbidden to anon → still must go through the
    FastAPI backend.
  - Creates `dashboard_properties` view (Property + first-search annotation,
    only "synced" properties, non-duplicates) — replaces the work the
    `GET /api/v1/properties` endpoint does in Python.
  - Creates `outreach_kanban` view (OutreachQueue joined with Property)
    — replaces `GET /api/v1/outreach`.
  - Creates `analytics_dashboard()` SQL function returning the same JSON
    shape as `GET /api/v1/analytics/dashboard`.
  - Adds a GIN index on search_history.result_property_ids so the lateral
    subquery in `dashboard_properties` stays fast.

Views use `security_invoker = true` so the caller's RLS (anon, in the
frontend's case) is what's enforced — not the view-creator's permissions.

Skipped on SQLite (test mode) since RLS + JSONB + security_invoker views
are Postgres-only.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.engine import Connection

revision: str = "b3f5a9c1d702"
down_revision: str | None = "a2c1d8f6e433"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_READ_TABLES = (
    "properties",
    "property_contacts",
    "outreach_queue",
    "search_history",
    "search_jobs",
    "query_bank",
    "sources",
)


def _is_postgres(conn: Connection) -> bool:
    return conn.dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()
    if not _is_postgres(conn):
        return

    # 1. GIN index for fast jsonb-array containment lookups on search history.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_history_result_ids_gin "
        "ON search_history USING GIN (result_property_ids jsonb_path_ops)"
    )

    # 2. Enable RLS + grant SELECT to anon on each read-exposed table.
    for tbl in _READ_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        # Drop-then-create so the migration is idempotent if re-run.
        op.execute(f"DROP POLICY IF EXISTS anon_read_{tbl} ON {tbl}")
        op.execute(
            f"CREATE POLICY anon_read_{tbl} ON {tbl} "
            "FOR SELECT TO anon USING (true)"
        )
        op.execute(f"GRANT SELECT ON {tbl} TO anon")

    # 3. dashboard_properties view — non-duplicate properties surfaced by at
    # least one user search, annotated with the first search that surfaced
    # them. Mirrors PropertyService.list_for_dashboard + first_search_by_property.
    op.execute("DROP VIEW IF EXISTS dashboard_properties")
    op.execute(
        """
        CREATE VIEW dashboard_properties
        WITH (security_invoker = true) AS
        SELECT
            p.id,
            p.canonical_name,
            p.normalized_name,
            p.normalized_address,
            p.city,
            p.locality,
            p.state,
            p.pincode,
            p.lat,
            p.lng,
            p.property_type,
            p.status,
            p.canonical_website,
            p.canonical_phone,
            p.canonical_email,
            p.short_brief,
            p.brief_generated_at,
            p.relevance_score,
            p.score_reason_json,
            p.scored_at,
            p.features_json,
            p.google_place_id,
            p.google_rating,
            p.google_review_count,
            p.duplicate_of,
            p.is_duplicate,
            p.created_at,
            p.updated_at,
            fsq.id            AS source_query_id,
            fsq.query_text    AS source_query_text
        FROM properties p
        JOIN LATERAL (
            SELECT sh.id, sh.query_text, sh.first_searched_at
            FROM search_history sh
            WHERE sh.result_property_ids ? (p.id::text)
            ORDER BY sh.first_searched_at ASC
            LIMIT 1
        ) fsq ON TRUE
        WHERE p.is_duplicate = false
        """
    )
    op.execute("GRANT SELECT ON dashboard_properties TO anon")

    # 4. outreach_kanban view — OutreachQueue with the property fields the
    # kanban card needs nested as a jsonb sub-object.
    op.execute("DROP VIEW IF EXISTS outreach_kanban")
    op.execute(
        """
        CREATE VIEW outreach_kanban
        WITH (security_invoker = true) AS
        SELECT
            o.id,
            o.property_id,
            o.status,
            o.priority,
            o.outreach_channel,
            o.suggested_angle,
            o.contact_attempts,
            o.first_contact_at,
            o.last_contact_at,
            o.follow_up_at,
            o.notes,
            o.created_at,
            o.updated_at,
            jsonb_build_object(
                'id',                p.id,
                'canonical_name',    p.canonical_name,
                'city',              p.city,
                'property_type',     p.property_type,
                'relevance_score',   p.relevance_score,
                'canonical_phone',   p.canonical_phone,
                'canonical_email',   p.canonical_email
            ) AS property
        FROM outreach_queue o
        JOIN properties p ON p.id = o.property_id
        """
    )
    op.execute("GRANT SELECT ON outreach_kanban TO anon")

    # 5. analytics_dashboard() — single-call replacement for AnalyticsService.
    op.execute("DROP FUNCTION IF EXISTS analytics_dashboard()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION analytics_dashboard()
        RETURNS jsonb
        LANGUAGE sql
        STABLE
        SECURITY INVOKER
        AS $$
            WITH
            prop_total AS (
                SELECT COUNT(*)::bigint AS n
                FROM properties WHERE is_duplicate = false
            ),
            prop_status AS (
                SELECT jsonb_object_agg(status, n) AS m FROM (
                    SELECT status, COUNT(*)::bigint AS n
                    FROM properties WHERE is_duplicate = false
                    GROUP BY status
                ) s
            ),
            prop_city AS (
                SELECT jsonb_object_agg(city, n) AS m FROM (
                    SELECT city, COUNT(*)::bigint AS n
                    FROM properties WHERE is_duplicate = false
                    GROUP BY city
                    ORDER BY n DESC
                    LIMIT 10
                ) c
            ),
            prop_type AS (
                SELECT jsonb_object_agg(property_type, n) AS m FROM (
                    SELECT property_type, COUNT(*)::bigint AS n
                    FROM properties WHERE is_duplicate = false
                    GROUP BY property_type
                    ORDER BY n DESC
                ) t
            ),
            outreach_status AS (
                SELECT
                    COALESCE(SUM(n) FILTER (WHERE status = 'pending'), 0)::bigint
                        AS pending,
                    COALESCE(SUM(n) FILTER (WHERE status = 'converted'), 0)::bigint
                        AS converted,
                    COALESCE(SUM(n) FILTER (WHERE status IN
                        ('contacted','responded','follow_up','no_response')), 0)::bigint
                        AS in_progress
                FROM (
                    SELECT status, COUNT(*)::bigint AS n
                    FROM outreach_queue GROUP BY status
                ) o
            ),
            llm AS (
                SELECT
                    COUNT(*) FILTER (WHERE scored_at IS NOT NULL)::bigint AS scored,
                    COUNT(*) FILTER (WHERE brief_generated_at IS NOT NULL)::bigint AS briefed
                FROM properties WHERE is_duplicate = false
            )
            SELECT jsonb_build_object(
                'properties', jsonb_build_object(
                    'total',     (SELECT n FROM prop_total),
                    'by_status', COALESCE((SELECT m FROM prop_status), '{}'::jsonb),
                    'by_city',   COALESCE((SELECT m FROM prop_city),   '{}'::jsonb),
                    'by_type',   COALESCE((SELECT m FROM prop_type),   '{}'::jsonb)
                ),
                'outreach', jsonb_build_object(
                    'pending',     (SELECT pending     FROM outreach_status),
                    'in_progress', (SELECT in_progress FROM outreach_status),
                    'converted',   (SELECT converted   FROM outreach_status)
                ),
                'llm', jsonb_build_object(
                    'scored',  (SELECT scored  FROM llm),
                    'briefed', (SELECT briefed FROM llm)
                )
            );
        $$
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION analytics_dashboard() TO anon")


def downgrade() -> None:
    conn = op.get_bind()
    if not _is_postgres(conn):
        return

    op.execute("DROP FUNCTION IF EXISTS analytics_dashboard()")
    op.execute("DROP VIEW IF EXISTS outreach_kanban")
    op.execute("DROP VIEW IF EXISTS dashboard_properties")

    for tbl in _READ_TABLES:
        op.execute(f"DROP POLICY IF EXISTS anon_read_{tbl} ON {tbl}")
        op.execute(f"REVOKE SELECT ON {tbl} FROM anon")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP INDEX IF EXISTS idx_search_history_result_ids_gin")
