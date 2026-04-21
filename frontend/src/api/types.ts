/**
 * TypeScript mirror of the backend Pydantic schemas.
 * Keep these in sync manually for now; in M9c we can auto-generate
 * from the FastAPI OpenAPI spec.
 */

export type PropertyStatus =
  | "new"
  | "reviewed"
  | "approved"
  | "rejected"
  | "onboarded"
  | "do_not_contact";

export type PropertyType =
  | "boutique_hotel"
  | "villa"
  | "bungalow"
  | "heritage_home"
  | "farmhouse"
  | "resort"
  | "banquet_hall"
  | "cafe"
  | "restaurant"
  | "warehouse"
  | "industrial_shed"
  | "office_space"
  | "school_campus"
  | "coworking_space"
  | "rooftop_venue"
  | "theatre_studio"
  | "club_lounge"
  | "other";

export interface PropertyListItem {
  id: string;
  canonical_name: string;
  city: string;
  locality: string | null;
  property_type: PropertyType;
  status: PropertyStatus;
  relevance_score: number | null;
  short_brief: string | null;
  canonical_phone: string | null;
  canonical_email: string | null;
  canonical_website: string | null;
  google_rating: number | null;

  source_query_id: string | null;
  source_query_text: string | null;
}

export interface Contact {
  id: string;
  contact_type: "phone" | "email" | "whatsapp" | "form" | "website" | "instagram";
  contact_value: string;
  normalized_value: string;
  source_name: string;
  source_url: string | null;
  extraction_method: string | null;
  confidence: number;
  is_public_business_contact: boolean;
  is_primary: boolean;
  flagged_personal: boolean;
}

export interface ScoreReason {
  sub_scores: Array<{
    name: string;
    value: number;
    weight: number;
    source: "deterministic" | "llm" | "fallback";
    reasoning: string;
  }>;
  weights: Record<string, number>;
}

export interface PropertyOutreach {
  id: string;
  status: OutreachStatus;
  priority: number;
  outreach_channel: OutreachChannel | null;
  contact_attempts: number;
  notes: string | null;
}

export interface PropertyDetail extends PropertyListItem {
  normalized_address: string | null;
  state: string | null;
  pincode: string | null;
  lat: number | null;
  lng: number | null;
  score_reason_json: ScoreReason | null;
  features_json: Record<string, unknown>;
  google_place_id: string | null;
  google_review_count: number | null;
  is_duplicate: boolean;
  duplicate_of: string | null;
  created_at: string;
  updated_at: string;
  normalized_name: string;

  contacts: Contact[];
  outreach: PropertyOutreach | null;
}

export type ReviewAction =
  | "approve"
  | "reject"
  | "do_not_contact"
  | "merge"
  | "reopen";

export interface ReviewRequest {
  action: ReviewAction;
  notes?: string;
  merge_into_id?: string;
}

export interface ReviewResponse {
  property_id: string;
  status: string;
  action_applied: ReviewAction;
  outreach_created: boolean;
  merged_into_id: string | null;
  dnc_entries_added: number;
}

export type OutreachStatus =
  | "pending"
  | "contacted"
  | "responded"
  | "follow_up"
  | "converted"
  | "declined"
  | "no_response";

export type OutreachChannel =
  | "phone"
  | "email"
  | "whatsapp"
  | "form"
  | "in_person";

export interface OutreachItem {
  id: string;
  status: OutreachStatus;
  priority: number;
  outreach_channel: OutreachChannel | null;
  suggested_angle: string | null;
  contact_attempts: number;
  first_contact_at: string | null;
  last_contact_at: string | null;
  follow_up_at: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  property: {
    id: string;
    canonical_name: string;
    city: string;
    property_type: string;
    relevance_score: number | null;
    canonical_phone: string | null;
    canonical_email: string | null;
  };
}

export interface OutreachStats {
  total: number;
  by_status: Record<string, number>;
  conversion_rate: number;
  avg_contact_attempts: number;
}

export interface Paginated<T> {
  data: T[];
  meta: {
    total_count: number;
    offset: number;
    page_size: number;
    has_next: boolean;
  };
}

export interface AnalyticsDashboard {
  properties: {
    total: number;
    by_status: Record<string, number>;
    by_city: Record<string, number>;
    by_type: Record<string, number>;
  };
  outreach: {
    pending: number;
    in_progress: number;
    converted: number;
  };
  llm: {
    scored: number;
    briefed: number;
  };
}

export interface ApiError {
  errors: Array<{ code: string; message: string; field: string | null }>;
}

// --- Public search (product pivot) ---

export interface SearchRequest {
  query: string;
  city?: string;
  property_type?: PropertyType;
  max_results?: number;
  use_airbnb?: boolean | null;
  use_magicbricks?: boolean | null;
  use_acres99?: boolean | null;
  refresh?: boolean;
  additional_results?: number | null;
}

export interface SearchSubScore {
  name: string;
  value: number;
  weight: number;
  source: "deterministic" | "llm" | "fallback";
}

export interface SearchResultItem {
  id: string;
  canonical_name: string;
  city: string;
  locality: string | null;
  property_type: string;
  relevance_score: number | null;
  short_brief: string | null;
  canonical_phone: string | null;
  canonical_email: string | null;
  canonical_website: string | null;
  google_rating: number | null;
  google_review_count: number | null;
  sub_scores: SearchSubScore[];
  features: Record<string, unknown>;
  // Surfaced separately from `features` so the card doesn't have to dig.
  // External-source rows (Airbnb, MagicBricks) populate all three; Google-
  // Places rows leave them null and use `canonical_website` for their
  // actual site link.
  // `source_label` drives the "View on {label} ↗" pill rendering.
  primary_image_url: string | null;
  external_url: string | null;
  source_label: string | null;

  source_query_id: string | null;
  source_query_text: string | null;
}

export interface SearchResponse {
  query: string;
  inferred_city: string | null;
  inferred_property_type: string | null;
  results: SearchResultItem[];
  candidates_discovered: number;
  candidates_new: number;
  candidates_skipped_known: number;
  duration_seconds: number;
  errors: string[];
}

export type SearchJobStatus = "running" | "completed" | "failed";

export interface SearchJob {
  id: string;
  query_text: string;
  status: SearchJobStatus;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SearchJobDetail extends SearchJob {
  response: SearchResponse | null;
}

export interface SearchHistoryItem {
  id: string;
  query_text: string;
  inferred_city: string | null;
  inferred_property_type: string | null;
  result_count: number;
  search_count: number;
  last_searched_at: string;
}

export interface SearchHistorySuggestion {
  query_text: string;
  result_count: number;
  last_searched_at: string;
}
