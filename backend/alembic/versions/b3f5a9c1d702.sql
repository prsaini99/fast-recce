-- ============================================================
-- Migration b3f5a9c1d702 — Supabase public-read RLS + views + RPC
-- Paste-and-run version of the alembic migration.
--
-- Safe to re-run: every statement is idempotent (DROP IF EXISTS,
-- CREATE OR REPLACE, IF NOT EXISTS).
--
-- After running, mark the migration as applied so alembic doesn't
-- try to re-run it next time:
--   UPDATE alembic_version SET version_num = 'b3f5a9c1d702';
-- ============================================================

BEGIN;

-- 1. GIN index for fast jsonb-array containment lookups on search history.
CREATE INDEX IF NOT EXISTS idx_search_history_result_ids_gin
    ON search_history USING GIN (result_property_ids jsonb_path_ops);

-- 2. Enable RLS + grant SELECT to anon on each read-exposed table.
ALTER TABLE properties        ENABLE ROW LEVEL SECURITY;
ALTER TABLE property_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE outreach_queue    ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_history    ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_jobs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_bank        ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources           ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS anon_read_properties        ON properties;
DROP POLICY IF EXISTS anon_read_property_contacts ON property_contacts;
DROP POLICY IF EXISTS anon_read_outreach_queue    ON outreach_queue;
DROP POLICY IF EXISTS anon_read_search_history    ON search_history;
DROP POLICY IF EXISTS anon_read_search_jobs       ON search_jobs;
DROP POLICY IF EXISTS anon_read_query_bank        ON query_bank;
DROP POLICY IF EXISTS anon_read_sources           ON sources;

CREATE POLICY anon_read_properties        ON properties        FOR SELECT TO anon USING (true);
CREATE POLICY anon_read_property_contacts ON property_contacts FOR SELECT TO anon USING (true);
CREATE POLICY anon_read_outreach_queue    ON outreach_queue    FOR SELECT TO anon USING (true);
CREATE POLICY anon_read_search_history    ON search_history    FOR SELECT TO anon USING (true);
CREATE POLICY anon_read_search_jobs       ON search_jobs       FOR SELECT TO anon USING (true);
CREATE POLICY anon_read_query_bank        ON query_bank        FOR SELECT TO anon USING (true);
CREATE POLICY anon_read_sources           ON sources           FOR SELECT TO anon USING (true);

GRANT SELECT ON properties        TO anon;
GRANT SELECT ON property_contacts TO anon;
GRANT SELECT ON outreach_queue    TO anon;
GRANT SELECT ON search_history    TO anon;
GRANT SELECT ON search_jobs       TO anon;
GRANT SELECT ON query_bank        TO anon;
GRANT SELECT ON sources           TO anon;

-- 3. dashboard_properties view — non-duplicate properties surfaced by at
-- least one user search, annotated with the first surfacing query.
DROP VIEW IF EXISTS dashboard_properties;
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
    fsq.id         AS source_query_id,
    fsq.query_text AS source_query_text
FROM properties p
JOIN LATERAL (
    SELECT sh.id, sh.query_text, sh.first_searched_at
    FROM search_history sh
    WHERE sh.result_property_ids ? (p.id::text)
    ORDER BY sh.first_searched_at ASC
    LIMIT 1
) fsq ON TRUE
WHERE p.is_duplicate = false;

GRANT SELECT ON dashboard_properties TO anon;

-- 4. outreach_kanban view — OutreachQueue with embedded property fields.
DROP VIEW IF EXISTS outreach_kanban;
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
JOIN properties p ON p.id = o.property_id;

GRANT SELECT ON outreach_kanban TO anon;

-- 5. analytics_dashboard() — single-call replacement for AnalyticsService.
DROP FUNCTION IF EXISTS analytics_dashboard();
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
            COALESCE(SUM(n) FILTER (WHERE status = 'pending'), 0)::bigint AS pending,
            COALESCE(SUM(n) FILTER (WHERE status = 'converted'), 0)::bigint AS converted,
            COALESCE(SUM(n) FILTER (WHERE status IN
                ('contacted','responded','follow_up','no_response')), 0)::bigint AS in_progress
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
$$;

GRANT EXECUTE ON FUNCTION analytics_dashboard() TO anon;

-- 6. Tell alembic this revision is applied (so a future
-- `alembic upgrade head` doesn't try to run it again).
UPDATE alembic_version SET version_num = 'b3f5a9c1d702';

COMMIT;
