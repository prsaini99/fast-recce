import { http } from "./client";
import type {
  AnalyticsDashboard,
  OutreachItem,
  OutreachStats,
  Paginated,
  PropertyDetail,
  PropertyListItem,
  ReviewRequest,
  ReviewResponse,
  SearchHistoryItem,
  SearchHistorySuggestion,
  SearchJob,
  SearchJobDetail,
  SearchJobStatus,
  SearchRequest,
} from "./types";

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

export const propertiesApi = {
  list: (params: PropertyListParams) =>
    http
      .get<Paginated<PropertyListItem>>("/properties", { params })
      .then((r) => r.data),
  get: (id: string) =>
    http.get<PropertyDetail>(`/properties/${id}`).then((r) => r.data),
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

export interface OutreachListParams {
  status?: string;
  city?: string;
  min_priority?: number;
  sort?: string;
  offset?: number;
  page_size?: number;
}

export const outreachApi = {
  list: (params: OutreachListParams) =>
    http
      .get<Paginated<OutreachItem>>("/outreach", { params })
      .then((r) => r.data),
  update: (id: string, body: Record<string, unknown>) =>
    http.patch<OutreachItem>(`/outreach/${id}`, body).then((r) => r.data),
  stats: (city?: string) =>
    http
      .get<OutreachStats>("/outreach/stats", { params: { city } })
      .then((r) => r.data),
};

export const analyticsApi = {
  dashboard: () =>
    http.get<AnalyticsDashboard>("/analytics/dashboard").then((r) => r.data),
};

export const searchApi = {
  /**
   * Kick off (or coalesce to) a search job. Always returns a job record;
   * callers poll `getJob(id)` until `status` flips to completed/failed.
   */
  start: (body: SearchRequest) =>
    http.post<SearchJob>("/search", body).then((r) => r.data),
  getJob: (id: string) =>
    http.get<SearchJobDetail>(`/search/jobs/${id}`).then((r) => r.data),
  listJobs: (status?: SearchJobStatus, limit = 50) =>
    http
      .get<SearchJob[]>("/search/jobs", { params: { status, limit } })
      .then((r) => r.data),
  cancelJob: (id: string) =>
    http.delete<SearchJob>(`/search/jobs/${id}`).then((r) => r.data),
  getProperty: (id: string) =>
    http.get<PropertyDetail>(`/search/property/${id}`).then((r) => r.data),
  history: (limit = 50) =>
    http
      .get<SearchHistoryItem[]>("/search/history", { params: { limit } })
      .then((r) => r.data),
  suggest: (q: string, limit = 8) =>
    http
      .get<SearchHistorySuggestion[]>("/search/history/suggest", {
        params: { q, limit },
      })
      .then((r) => r.data),
};
