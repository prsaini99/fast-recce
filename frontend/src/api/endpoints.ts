/**
 * API surface for the frontend.
 *
 * Read-only listings (history, properties, outreach, analytics) talk to
 * Supabase PostgREST directly so a sleeping Render dyno never blocks the
 * UI. Mutations and live job polling still go through the FastAPI backend
 * via axios — those endpoints do real work (scraping, LLM calls).
 *
 * Function signatures and return types stay 1:1 with the previous Render-
 * only client so consuming pages don't change.
 */

import { http } from "./client";
import { supabase } from "@/lib/supabase";
import type {
  AnalyticsDashboard,
  Contact,
  OutreachItem,
  OutreachStats,
  Paginated,
  PropertyDetail,
  PropertyListItem,
  PropertyOutreach,
  ReviewRequest,
  ReviewResponse,
  SearchHistoryItem,
  SearchHistorySuggestion,
  SearchJob,
  SearchJobDetail,
  SearchJobStatus,
  SearchRequest,
} from "./types";

// Internal raw row shapes — what PostgREST returns from each table/view.
// Kept loose (no shared schema generator yet) and narrowed at the
// boundary into the strict public types above.
type Row = Record<string, unknown>;

function unwrap<T>(value: { data: T | null; error: { message: string } | null }): T {
  if (value.error) throw new Error(value.error.message);
  if (value.data === null) throw new Error("supabase returned null data");
  return value.data;
}

// ---------- properties ----------

export interface PropertyListParams {
  city?: string;
  property_type?: string;
  status?: string;
  min_score?: number;
  max_score?: number;
  has_phone?: boolean;
  has_email?: boolean;
  is_duplicate?: boolean;
  search?: string;
  sort?: string;
  offset?: number;
  page_size?: number;
}

const PROPERTY_LIST_COLUMNS = [
  "id",
  "canonical_name",
  "city",
  "locality",
  "property_type",
  "status",
  "relevance_score",
  "short_brief",
  "canonical_phone",
  "canonical_email",
  "canonical_website",
  "google_rating",
  "source_query_id",
  "source_query_text",
].join(",");

export const propertiesApi = {
  list: async (params: PropertyListParams): Promise<Paginated<PropertyListItem>> => {
    const offset = params.offset ?? 0;
    const pageSize = params.page_size ?? 50;

    let q: PgQuery = supabase
      .from("dashboard_properties")
      .select(PROPERTY_LIST_COLUMNS, { count: "exact" });

    if (params.city) q = q.eq("city", params.city);
    if (params.property_type) {
      const types = params.property_type.split(",").map((s) => s.trim()).filter(Boolean);
      if (types.length) q = q.in("property_type", types);
    }
    if (params.status) {
      const statuses = params.status.split(",").map((s) => s.trim()).filter(Boolean);
      if (statuses.length) q = q.in("status", statuses);
    }
    if (params.min_score != null) q = q.gte("relevance_score", params.min_score);
    if (params.max_score != null) q = q.lte("relevance_score", params.max_score);
    if (params.has_phone === true) q = q.not("canonical_phone", "is", null);
    if (params.has_phone === false) q = q.is("canonical_phone", null);
    if (params.has_email === true) q = q.not("canonical_email", "is", null);
    if (params.has_email === false) q = q.is("canonical_email", null);
    if (params.search) {
      const needle = params.search.replace(/[%]/g, "");
      // OR across canonical_name / locality / city — PostgREST `or()` filter.
      q = q.or(
        [
          `canonical_name.ilike.%${needle}%`,
          `locality.ilike.%${needle}%`,
          `city.ilike.%${needle}%`,
        ].join(","),
      );
    }

    q = applyPropertySort(q, params.sort ?? "relevance_score_desc");
    q = q.range(offset, offset + pageSize - 1);

    const { data, count, error } = await q;
    if (error) throw new Error(error.message);
    const rows = (data ?? []) as Row[];
    const total = count ?? 0;

    return {
      data: rows.map(toPropertyListItem),
      meta: {
        total_count: total,
        offset,
        page_size: pageSize,
        has_next: offset + rows.length < total,
      },
    };
  },

  get: async (id: string): Promise<PropertyDetail> =>
    fetchPropertyDetail(id, /* withOutreach */ true),

  // --- Mutations stay on Render. ---

  review: (id: string, body: ReviewRequest) =>
    http
      .patch<ReviewResponse>(`/properties/${id}/review`, body)
      .then((r) => r.data),
  score: (id: string) =>
    http.post<PropertyDetail>(`/properties/${id}/score`).then((r) => r.data),
  brief: (id: string) =>
    http.post<PropertyDetail>(`/properties/${id}/brief`).then((r) => r.data),
  enrich: (id: string) =>
    http.post<PropertyDetail>(`/properties/${id}/enrich`).then((r) => r.data),
};

// ---------- outreach ----------

export interface OutreachListParams {
  status?: string;
  city?: string;
  min_priority?: number;
  sort?: string;
  offset?: number;
  page_size?: number;
}

export const outreachApi = {
  list: async (params: OutreachListParams): Promise<Paginated<OutreachItem>> => {
    const offset = params.offset ?? 0;
    const pageSize = params.page_size ?? 50;

    let q: PgQuery = supabase
      .from("outreach_kanban")
      .select("*", { count: "exact" });

    if (params.status) {
      const statuses = params.status.split(",").map((s) => s.trim()).filter(Boolean);
      if (statuses.length) q = q.in("status", statuses);
    }
    if (params.min_priority != null) q = q.gte("priority", params.min_priority);
    // City filter applies to the embedded property — PostgREST doesn't filter
    // jsonb sub-objects, so we filter client-side after fetch. Acceptable
    // because the kanban is small (<200 rows in practice).
    q = applyOutreachSort(q, params.sort ?? "priority_desc");
    q = q.range(offset, offset + pageSize - 1);

    const { data, count, error } = await q;
    if (error) throw new Error(error.message);
    let rows = (data ?? []) as Row[];

    if (params.city) {
      rows = rows.filter((r) => {
        const prop = r.property as Row | null;
        return prop?.city === params.city;
      });
    }

    return {
      data: rows.map(toOutreachItem),
      meta: {
        total_count: count ?? 0,
        offset,
        page_size: pageSize,
        has_next: offset + rows.length < (count ?? 0),
      },
    };
  },

  // PATCH still goes through Render (writes the row, recomputes priority,
  // updates contact-attempt counters).
  update: (id: string, body: Record<string, unknown>) =>
    http.patch<OutreachItem>(`/outreach/${id}`, body).then((r) => r.data),

  stats: async (city?: string): Promise<OutreachStats> => {
    // Stats over the (small) kanban view. Pull the rows we need and
    // aggregate locally — saves us a second RPC.
    const q: PgQuery = supabase
      .from("outreach_kanban")
      .select("status,contact_attempts,property");
    const { data, error } = await q;
    if (error) throw new Error(error.message);

    const rows = (data ?? []) as Row[];
    const filtered = city
      ? rows.filter((r) => (r.property as Row | null)?.city === city)
      : rows;

    const by_status: Record<string, number> = {};
    let total_attempts = 0;
    for (const r of filtered) {
      const s = String(r.status);
      by_status[s] = (by_status[s] ?? 0) + 1;
      total_attempts += Number(r.contact_attempts ?? 0);
    }
    const total = filtered.length;
    const converted = by_status["converted"] ?? 0;
    return {
      total,
      by_status,
      conversion_rate: total > 0 ? converted / total : 0,
      avg_contact_attempts: total > 0 ? total_attempts / total : 0,
    };
  },
};

// ---------- analytics ----------

export const analyticsApi = {
  dashboard: async (): Promise<AnalyticsDashboard> => {
    const { data, error } = await supabase.rpc("analytics_dashboard");
    if (error) throw new Error(error.message);
    return data as AnalyticsDashboard;
  },
};

// ---------- search ----------

export const searchApi = {
  // --- Job-creating / live-job operations stay on Render. ---

  start: (body: SearchRequest) =>
    http.post<SearchJob>("/search", body).then((r) => r.data),
  getJob: (id: string) =>
    http.get<SearchJobDetail>(`/search/jobs/${id}`).then((r) => r.data),
  cancelJob: (id: string) =>
    http.delete<SearchJob>(`/search/jobs/${id}`).then((r) => r.data),

  // --- Listings + cached reads go straight to Supabase. ---

  listJobs: async (
    status?: SearchJobStatus,
    limit = 50,
  ): Promise<SearchJob[]> => {
    let q: PgQuery = supabase
      .from("search_jobs")
      .select("id,query_text,status,error,started_at,finished_at")
      .order("started_at", { ascending: false })
      .limit(limit);
    if (status) q = q.eq("status", status);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as SearchJob[];
  },

  getProperty: (id: string): Promise<PropertyDetail> =>
    fetchPropertyDetail(id, /* withOutreach */ false),

  history: async (limit = 50): Promise<SearchHistoryItem[]> => {
    const { data, error } = await supabase
      .from("search_history")
      .select(
        "id,query_text,inferred_city,inferred_property_type,result_property_ids,search_count,last_searched_at",
      )
      .order("last_searched_at", { ascending: false })
      .limit(limit);
    if (error) throw new Error(error.message);
    const rows = (data ?? []) as Row[];
    return rows.map((r) => ({
      id: String(r.id),
      query_text: String(r.query_text),
      inferred_city: (r.inferred_city as string | null) ?? null,
      inferred_property_type: (r.inferred_property_type as string | null) ?? null,
      result_count: Array.isArray(r.result_property_ids)
        ? r.result_property_ids.length
        : 0,
      search_count: Number(r.search_count ?? 0),
      last_searched_at: String(r.last_searched_at),
    }));
  },

  suggest: async (q: string, limit = 8): Promise<SearchHistorySuggestion[]> => {
    const needle = q.trim().toLowerCase().replace(/\s+/g, " ");
    if (!needle) return [];
    const escaped = needle.replace(/[%_]/g, "\\$&");
    const { data, error } = await supabase
      .from("search_history")
      .select("query_text,result_property_ids,last_searched_at,search_count")
      .like("normalized_query", `${escaped}%`)
      .order("search_count", { ascending: false })
      .order("last_searched_at", { ascending: false })
      .limit(limit);
    if (error) throw new Error(error.message);
    const rows = (data ?? []) as Row[];
    return rows.map((r) => ({
      query_text: String(r.query_text),
      result_count: Array.isArray(r.result_property_ids)
        ? r.result_property_ids.length
        : 0,
      last_searched_at: String(r.last_searched_at),
    }));
  },
};

// ---------- internal helpers ----------

async function fetchPropertyDetail(
  id: string,
  withOutreach: boolean,
): Promise<PropertyDetail> {
  // Property + embedded contacts + (optionally) outreach in one round-trip
  // using PostgREST resource embedding via the FK relationships.
  const select = withOutreach
    ? "*, property_contacts(*), outreach_queue(*)"
    : "*, property_contacts(*)";

  // Cast to `any` — the embedded-select string ("*, property_contacts(*),
  // outreach_queue(*)") relies on FKs PostgREST resolves at runtime; the
  // untyped supabase-js client tries to validate it at compile time and
  // fails because we haven't generated database types.
  const { data, error } = await (supabase
    .from("properties")
    .select(select as any)
    .eq("id", id)
    .single() as any);
  if (error) throw new Error(error.message);
  const row = data as Row;

  const contacts: Contact[] = ((row.property_contacts as Row[]) ?? [])
    .map((c) => ({
      id: String(c.id),
      contact_type: c.contact_type as Contact["contact_type"],
      contact_value: String(c.contact_value),
      normalized_value: String(c.normalized_value),
      source_name: String(c.source_name),
      source_url: (c.source_url as string | null) ?? null,
      extraction_method: (c.extraction_method as string | null) ?? null,
      confidence: Number(c.confidence ?? 0),
      is_public_business_contact: Boolean(c.is_public_business_contact),
      is_primary: Boolean(c.is_primary),
      flagged_personal: Boolean(c.flagged_personal),
    }))
    .sort((a, b) => b.confidence - a.confidence);

  let outreach: PropertyOutreach | null = null;
  if (withOutreach) {
    const oq = row.outreach_queue as Row[] | Row | undefined;
    const o = Array.isArray(oq) ? oq[0] : oq;
    if (o) {
      outreach = {
        id: String(o.id),
        status: o.status as PropertyOutreach["status"],
        priority: Number(o.priority ?? 0),
        outreach_channel: (o.outreach_channel as PropertyOutreach["outreach_channel"]) ?? null,
        contact_attempts: Number(o.contact_attempts ?? 0),
        notes: (o.notes as string | null) ?? null,
      };
    }
  }

  // Detail rows aren't surfaced via the dashboard view, so source_query_*
  // is unknown here. PropertyDetail allows null for these fields.
  return {
    id: String(row.id),
    canonical_name: String(row.canonical_name),
    city: String(row.city),
    locality: (row.locality as string | null) ?? null,
    property_type: row.property_type as PropertyDetail["property_type"],
    status: row.status as PropertyDetail["status"],
    relevance_score: (row.relevance_score as number | null) ?? null,
    short_brief: (row.short_brief as string | null) ?? null,
    canonical_phone: (row.canonical_phone as string | null) ?? null,
    canonical_email: (row.canonical_email as string | null) ?? null,
    canonical_website: (row.canonical_website as string | null) ?? null,
    google_rating: (row.google_rating as number | null) ?? null,
    source_query_id: null,
    source_query_text: null,
    normalized_address: (row.normalized_address as string | null) ?? null,
    state: (row.state as string | null) ?? null,
    pincode: (row.pincode as string | null) ?? null,
    lat: (row.lat as number | null) ?? null,
    lng: (row.lng as number | null) ?? null,
    score_reason_json: (row.score_reason_json as PropertyDetail["score_reason_json"]) ?? null,
    features_json: (row.features_json as Record<string, unknown>) ?? {},
    google_place_id: (row.google_place_id as string | null) ?? null,
    google_review_count: (row.google_review_count as number | null) ?? null,
    is_duplicate: Boolean(row.is_duplicate),
    duplicate_of: (row.duplicate_of as string | null) ?? null,
    created_at: String(row.created_at),
    updated_at: String(row.updated_at),
    normalized_name: String(row.normalized_name),
    contacts,
    outreach,
  };
}

function toPropertyListItem(r: Row): PropertyListItem {
  return {
    id: String(r.id),
    canonical_name: String(r.canonical_name),
    city: String(r.city),
    locality: (r.locality as string | null) ?? null,
    property_type: r.property_type as PropertyListItem["property_type"],
    status: r.status as PropertyListItem["status"],
    relevance_score: (r.relevance_score as number | null) ?? null,
    short_brief: (r.short_brief as string | null) ?? null,
    canonical_phone: (r.canonical_phone as string | null) ?? null,
    canonical_email: (r.canonical_email as string | null) ?? null,
    canonical_website: (r.canonical_website as string | null) ?? null,
    google_rating: (r.google_rating as number | null) ?? null,
    source_query_id: (r.source_query_id as string | null) ?? null,
    source_query_text: (r.source_query_text as string | null) ?? null,
  };
}

function toOutreachItem(r: Row): OutreachItem {
  const p = (r.property as Row) ?? {};
  return {
    id: String(r.id),
    status: r.status as OutreachItem["status"],
    priority: Number(r.priority ?? 0),
    outreach_channel: (r.outreach_channel as OutreachItem["outreach_channel"]) ?? null,
    suggested_angle: (r.suggested_angle as string | null) ?? null,
    contact_attempts: Number(r.contact_attempts ?? 0),
    first_contact_at: (r.first_contact_at as string | null) ?? null,
    last_contact_at: (r.last_contact_at as string | null) ?? null,
    follow_up_at: (r.follow_up_at as string | null) ?? null,
    notes: (r.notes as string | null) ?? null,
    created_at: String(r.created_at),
    updated_at: String(r.updated_at),
    property: {
      id: String(p.id ?? ""),
      canonical_name: String(p.canonical_name ?? ""),
      city: String(p.city ?? ""),
      property_type: String(p.property_type ?? ""),
      relevance_score: (p.relevance_score as number | null) ?? null,
      canonical_phone: (p.canonical_phone as string | null) ?? null,
      canonical_email: (p.canonical_email as string | null) ?? null,
    },
  };
}

// supabase-js's PostgrestFilterBuilder uses very deep generic phantom types
// that don't cooperate with structural aliases. The helpers below only call
// `.order()` which exists on every variant, so `any` is the practical choice.
type PgQuery = any;

function applyPropertySort(q: PgQuery, sort: string): PgQuery {
  switch (sort) {
    case "relevance_score_desc":
      return q
        .order("relevance_score", { ascending: false, nullsFirst: false })
        .order("created_at", { ascending: false });
    case "relevance_score_asc":
      return q
        .order("relevance_score", { ascending: true, nullsFirst: true })
        .order("created_at", { ascending: false });
    case "created_at_asc":
      return q.order("created_at", { ascending: true });
    case "canonical_name_asc":
      return q.order("canonical_name", { ascending: true });
    case "created_at_desc":
    default:
      return q.order("created_at", { ascending: false });
  }
}

function applyOutreachSort(q: PgQuery, sort: string): PgQuery {
  switch (sort) {
    case "priority_asc":
      return q.order("priority", { ascending: true }).order("created_at");
    case "created_at_desc":
      return q.order("created_at", { ascending: false });
    case "follow_up_at_asc":
      return q.order("follow_up_at", { ascending: true, nullsFirst: false });
    case "priority_desc":
    default:
      return q.order("priority", { ascending: false }).order("created_at");
  }
}

// Re-exported but unused publicly; kept to satisfy potential external callers.
void unwrap;
