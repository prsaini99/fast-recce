import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { toast } from "sonner";

import { extractErrorMessage } from "@/api/client";
import { propertiesApi, searchApi } from "@/api/endpoints";
import type { SearchJob, SearchResultItem } from "@/api/types";
import { ScoreBadge } from "@/components/ScoreBadge";
import { loadSearchPrefs } from "@/components/SearchOptions";

const PROGRESS_STAGES = [
  "Searching Google Places…",
  "Searching Airbnb listings…",
  "Fetching place details…",
  "Extracting amenities and photos…",
  "Scoring and writing briefs…",
  "Assembling results…",
];

/**
 * Map of `normalized-query-text → job_id` pinned for the current tab.
 * Survives refreshes so remounting SearchResults doesn't re-POST /search
 * (which would either coalesce to the same job, or bump search_count on a
 * cache hit — neither is strictly wrong, just wasteful).
 */
const JOB_SESSION_PREFIX = "fastrecce.search.job.";

function pinnedJobId(query: string): string | null {
  try {
    return sessionStorage.getItem(JOB_SESSION_PREFIX + query.toLowerCase().trim());
  } catch {
    return null;
  }
}

function pinJobId(query: string, jobId: string): void {
  try {
    sessionStorage.setItem(
      JOB_SESSION_PREFIX + query.toLowerCase().trim(),
      jobId,
    );
  } catch {
    // storage disabled / quota — fall back to re-POSTing on next mount.
  }
}

function clearPinnedJobId(query: string): void {
  try {
    sessionStorage.removeItem(JOB_SESSION_PREFIX + query.toLowerCase().trim());
  } catch {
    // nothing to do.
  }
}

export function SearchResultsPage() {
  const [params] = useSearchParams();
  const query = params.get("q")?.trim() ?? "";
  const queryClient = useQueryClient();

  const [jobId, setJobId] = useState<string | null>(() => pinnedJobId(query));

  // Re-pin when the URL query changes (user clicks a different past search).
  useEffect(() => {
    setJobId(pinnedJobId(query));
  }, [query]);

  // Kick off the job exactly once per (query, missing pinned job).
  // React-Query's `useMutation` gives us onSuccess + idempotent re-mount
  // behavior when combined with the sessionStorage guard above.
  const startMutation = useMutation({
    mutationFn: (opts?: { refresh?: boolean }) => {
      const prefs = loadSearchPrefs();
      return searchApi.start({
        query,
        max_results: prefs.max_results,
        use_airbnb: prefs.use_airbnb,
        use_magicbricks: prefs.use_magicbricks,
        use_acres99: prefs.use_acres99,
        refresh: opts?.refresh ?? false,
      });
    },
    onSuccess: (job) => {
      pinJobId(query, job.id);
      setJobId(job.id);
      queryClient.invalidateQueries({ queryKey: ["search-jobs", "running"] });
      queryClient.invalidateQueries({ queryKey: ["search-history"] });
    },
  });

  // React 18 StrictMode double-invokes effects in dev. We guard with a ref
  // that holds the last query we fired for — a plain state-backed check
  // wouldn't work because `isPending` doesn't flip synchronously between
  // the two StrictMode invocations. The ref is NOT reset via a separate
  // effect (a reset-effect also re-runs on remount, nulling the guard);
  // instead we compare the ref to the current query inline. When the URL
  // query changes, ref still holds the PREVIOUS query, so the inequality
  // check succeeds and we fire for the new query exactly once.
  const firedForQueryRef = useRef<string | null>(null);

  useEffect(() => {
    if (query.length < 2) return;
    if (jobId) return;
    if (firedForQueryRef.current === query) return;
    firedForQueryRef.current = query;
    startMutation.mutate(undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, jobId]);

  // "Find more results" — user already has results for this query but wants
  // us to scrape again and union any new property IDs into the same
  // search_history row. We skip the cache by passing refresh=true and
  // adopt the new job id so the poll switches to the freshly-running job.
  function runRefresh() {
    firedForQueryRef.current = query; // prevent the auto-mount effect from re-firing
    clearPinnedJobId(query);
    setJobId(null);
    startMutation.mutate({ refresh: true });
  }

  // Poll the job. Stops polling once it reaches a terminal state.
  const jobQuery = useQuery({
    queryKey: ["search-job", jobId],
    queryFn: () => searchApi.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return 2000;
      return data.status === "running" ? 2000 : false;
    },
    staleTime: 0,
    retry: 0,
  });

  // When the job completes, also refresh the sidebar lists.
  const jobStatus = jobQuery.data?.status;
  useEffect(() => {
    if (jobStatus === "completed" || jobStatus === "failed") {
      queryClient.invalidateQueries({ queryKey: ["search-jobs", "running"] });
      queryClient.invalidateQueries({ queryKey: ["search-history"] });
    }
  }, [jobStatus, queryClient]);

  // If the pinned job turns out to be in a terminal failure state (e.g.
  // cancelled by user, or timed out on a prior session), drop the pin and
  // reset the fire-once ref so the next render re-kicks a fresh scrape
  // for this query. Without this, the user is stuck staring at a stale
  // error screen every time they land on the same search URL.
  useEffect(() => {
    if (jobStatus !== "failed") return;
    clearPinnedJobId(query);
    firedForQueryRef.current = null;
    setJobId(null);
  }, [jobStatus, query]);

  const runningSeconds = useRunningSeconds(
    jobQuery.data?.started_at,
    jobStatus === "running",
  );

  const canRefresh =
    query.length >= 2 &&
    jobStatus !== "running" &&
    !startMutation.isPending;

  return (
    <div>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <div className="text-xs text-muted-foreground">Results for</div>
          <h2 className="text-2xl font-semibold">"{query || "—"}"</h2>
        </div>
        {canRefresh && jobQuery.data?.response ? (
          <button
            type="button"
            onClick={runRefresh}
            title="Re-run the pipeline and add any new properties to this search"
            className="shrink-0 rounded-md border border-primary/50 bg-primary/10 px-3 py-1.5 text-xs font-medium hover:bg-primary/15"
          >
            ↻ Find more results
          </button>
        ) : null}
      </div>

      {query.length < 2 ? (
        <p className="text-sm text-muted-foreground">
          Enter a search in the top bar to see results.
        </p>
      ) : startMutation.isError ? (
        <ErrorState message={extractErrorMessage(startMutation.error)} />
      ) : jobQuery.isError ? (
        <ErrorState message={extractErrorMessage(jobQuery.error)} />
      ) : !jobQuery.data && !startMutation.data ? (
        <LoadingState seconds={runningSeconds} />
      ) : jobStatus === "running" ? (
        <LoadingState seconds={runningSeconds} />
      ) : jobStatus === "failed" ? (
        <ErrorState
          message={jobQuery.data?.error || "Search failed. Please try again."}
        />
      ) : jobQuery.data?.response ? (
        jobQuery.data.response.results.length === 0 ? (
          <EmptyState errors={jobQuery.data.response.errors} />
        ) : (
          <ResultsList
            results={jobQuery.data.response.results}
            inferredCity={jobQuery.data.response.inferred_city}
            duration={jobQuery.data.response.duration_seconds}
            errors={jobQuery.data.response.errors}
          />
        )
      ) : (
        <LoadingState seconds={runningSeconds} />
      )}
    </div>
  );
}

function useRunningSeconds(
  startedAtIso: string | undefined,
  active: boolean,
): number {
  const startedAt = useMemo(
    () => (startedAtIso ? new Date(startedAtIso).getTime() : null),
    [startedAtIso],
  );
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [active]);
  if (!startedAt) return 0;
  return Math.max(0, Math.floor((now - startedAt) / 1000));
}

function LoadingState({ seconds }: { seconds: number }) {
  const [stage, setStage] = useState(0);
  useEffect(() => {
    const id = setInterval(() => {
      setStage((s) => (s + 1) % PROGRESS_STAGES.length);
    }, 8000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-6">
      <div className="rounded-md border border-border bg-background p-6">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="h-3 w-3 animate-pulse rounded-full bg-primary" />
            <div className="text-sm font-medium">{PROGRESS_STAGES[stage]}</div>
          </div>
          <div className="tabular-nums text-xs text-muted-foreground">
            {seconds}s
          </div>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          The scrape is running in the background — you can refresh, close
          this tab, or start another search. Progress is preserved.
        </p>
      </div>
      <div className="space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-md bg-muted/40" />
        ))}
      </div>
    </div>
  );
}

function EmptyState({ errors }: { errors: string[] }) {
  return (
    <div className="rounded-md border border-dashed border-border bg-muted/20 p-10 text-center">
      <p className="text-sm font-medium">No results yet.</p>
      <p className="mt-2 text-xs text-muted-foreground">
        Try a query like <em>"boutique hotels in Lonavala"</em> — include both
        a property type and a city.
      </p>
      {errors.length > 0 ? (
        <details className="mt-4 text-left text-xs text-muted-foreground">
          <summary className="cursor-pointer">Diagnostics</summary>
          <ul className="mt-2 space-y-1">
            {errors.map((e, i) => (
              <li key={i}>• {e}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-500/40 bg-red-500/10 p-4 text-sm">
      Search failed: {message}
    </div>
  );
}

const GOOGLE_LABEL = "Google";

function sourceOf(r: SearchResultItem): string {
  return r.source_label ?? GOOGLE_LABEL;
}

const FILTER_STORAGE_KEY = "fastrecce.search.hiddenSources";

function loadHiddenSources(): Set<string> {
  try {
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed as string[]) : new Set();
  } catch {
    return new Set();
  }
}

function saveHiddenSources(hidden: Set<string>): void {
  try {
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify([...hidden]));
  } catch {
    // quota / disabled — the filter still works in-memory.
  }
}

function ResultsList({
  results,
  inferredCity,
  duration,
  errors,
}: {
  results: SearchResultItem[];
  inferredCity: string | null;
  duration: number;
  errors: string[];
}) {
  const queryClient = useQueryClient();
  const [hiddenSources, setHiddenSources] = useState<Set<string>>(() =>
    loadHiddenSources(),
  );

  // Bulk enrich — fires /properties/{id}/enrich for every visible result
  // in parallel (with a small concurrency cap so Gemini's free-tier rate
  // limit doesn't reject half the calls). Progress is reported in-situ so
  // the user sees "5/10" rather than a single spinner.
  const [bulkProgress, setBulkProgress] = useState<{
    done: number;
    total: number;
    failed: number;
  } | null>(null);
  const bulkRunning = bulkProgress !== null && bulkProgress.done < bulkProgress.total;

  async function runBulkEnrich(items: SearchResultItem[]) {
    if (items.length === 0 || bulkRunning) return;
    setBulkProgress({ done: 0, total: items.length, failed: 0 });

    const CONCURRENCY = 3;
    let failed = 0;
    let completed = 0;

    const queue = [...items];
    async function worker() {
      while (queue.length > 0) {
        const next = queue.shift();
        if (!next) break;
        try {
          await propertiesApi.enrich(next.id);
        } catch {
          failed += 1;
        }
        completed += 1;
        setBulkProgress({ done: completed, total: items.length, failed });
      }
    }
    await Promise.all(
      Array.from({ length: Math.min(CONCURRENCY, items.length) }, () => worker()),
    );

    if (failed === 0) {
      toast.success(`Enriched ${items.length} properties`);
    } else {
      toast.warning(`Enriched ${items.length - failed}/${items.length} (${failed} failed)`);
    }
    queryClient.invalidateQueries({ queryKey: ["search-job"] });
    queryClient.invalidateQueries({ queryKey: ["properties"] });
    // Give the "Done" state a beat before clearing.
    setTimeout(() => setBulkProgress(null), 2000);
  }

  const sourceCounts = new Map<string, number>();
  for (const r of results) {
    const key = sourceOf(r);
    sourceCounts.set(key, (sourceCounts.get(key) ?? 0) + 1);
  }
  const availableSources = [...sourceCounts.keys()].sort();

  const toggleSource = (source: string) => {
    setHiddenSources((prev) => {
      const next = new Set(prev);
      if (next.has(source)) next.delete(source);
      else next.add(source);
      saveHiddenSources(next);
      return next;
    });
  };

  const visibleResults = results.filter((r) => !hiddenSources.has(sourceOf(r)));

  const visibleUnenrichedCount = visibleResults.filter(
    (r) => r.relevance_score == null || !r.short_brief,
  ).length;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
        <span>
          {visibleResults.length} of {results.length} result(s)
          {inferredCity ? <> in <strong>{inferredCity}</strong></> : null}
          · scraped in {duration.toFixed(1)}s
        </span>
        <div className="flex items-center gap-3">
          {bulkProgress ? (
            <span className="tabular-nums text-muted-foreground">
              Enriching {bulkProgress.done}/{bulkProgress.total}
              {bulkProgress.failed > 0 ? ` · ${bulkProgress.failed} failed` : ""}
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => runBulkEnrich(visibleResults)}
            disabled={bulkRunning || visibleResults.length === 0}
            title={
              visibleUnenrichedCount === 0
                ? "All visible results already have scores + briefs — click to re-run"
                : `Run LLM score + brief on ${visibleResults.length} result(s)`
            }
            className="rounded-md border border-primary/50 bg-primary/10 px-3 py-1 text-xs font-medium text-foreground hover:bg-primary/15 disabled:opacity-50"
          >
            {bulkRunning
              ? "Enriching…"
              : visibleUnenrichedCount === 0
              ? "Re-enrich all"
              : `Enrich all (${visibleUnenrichedCount})`}
          </button>
          {errors.length > 0 ? (
            <details>
              <summary className="cursor-pointer">
                {errors.length} warning(s)
              </summary>
              <ul className="mt-2 space-y-1">
                {errors.map((e, i) => (
                  <li key={i}>• {e}</li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      </div>

      {availableSources.length > 1 ? (
        <SourceFilterBar
          sources={availableSources}
          counts={sourceCounts}
          hidden={hiddenSources}
          onToggle={toggleSource}
        />
      ) : null}

      <ul className="space-y-3">
        {visibleResults.map((r) => (
          <ResultCard key={r.id} result={r} />
        ))}
      </ul>
    </div>
  );
}

function SourceFilterBar({
  sources,
  counts,
  hidden,
  onToggle,
}: {
  sources: string[];
  counts: Map<string, number>;
  hidden: Set<string>;
  onToggle: (source: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-muted-foreground">Sources:</span>
      {sources.map((s) => {
        const isHidden = hidden.has(s);
        const count = counts.get(s) ?? 0;
        return (
          <button
            key={s}
            type="button"
            onClick={() => onToggle(s)}
            aria-pressed={!isHidden}
            className={
              "inline-flex items-center gap-1 rounded-full border px-3 py-1 transition-colors " +
              (isHidden
                ? "border-border bg-background text-muted-foreground hover:bg-muted"
                : "border-primary/50 bg-primary/10 text-foreground hover:bg-primary/15")
            }
          >
            <span className="h-2 w-2 rounded-full"
              style={{ backgroundColor: isHidden ? "transparent" : "currentColor" }}
            />
            {s} ({count})
          </button>
        );
      })}
    </div>
  );
}

function getScrapedDescription(result: SearchResultItem): string | null {
  const desc = result.features?.description;
  if (typeof desc !== "string") return null;
  const trimmed = desc.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function getAmenities(result: SearchResultItem): string[] {
  const amenities = result.features?.amenities;
  if (!Array.isArray(amenities)) return [];
  return amenities
    .filter((a): a is string => typeof a === "string" && a.trim().length > 0)
    .map((a) => a.trim());
}

/**
 * Compact facts row (price · BHK · baths · sqft · guests) assembled from
 * the scraper-side features. Every field is optional so a row missing
 * half the data still renders what we have.
 */
function getListingSpecifics(result: SearchResultItem): string[] {
  const f = result.features ?? {};
  const parts: string[] = [];
  if (typeof f.price_display === "string" && f.price_display) {
    parts.push(f.price_display);
  }
  if (typeof f.bedrooms === "number" && f.bedrooms > 0) {
    parts.push(`${f.bedrooms} BHK`);
  }
  if (typeof f.bathrooms === "number" && f.bathrooms > 0) {
    parts.push(`${f.bathrooms} bath${f.bathrooms === 1 ? "" : "s"}`);
  }
  if (typeof f.area_sqft === "number" && f.area_sqft > 0) {
    parts.push(`${Math.round(f.area_sqft)} sqft`);
  }
  if (typeof f.max_guests === "number" && f.max_guests > 0) {
    parts.push(`fits ${f.max_guests}`);
  }
  if (typeof f.property_subtype === "string" && f.property_subtype) {
    parts.push(f.property_subtype);
  }
  return parts;
}

function ResultCard({ result }: { result: SearchResultItem }) {
  const queryClient = useQueryClient();
  const enrichMutation = useMutation({
    mutationFn: () => propertiesApi.enrich(result.id),
    onSuccess: () => {
      toast.success("Scored + briefed");
      // Refresh whatever lists might be showing this property.
      queryClient.invalidateQueries({ queryKey: ["search-job"] });
      queryClient.invalidateQueries({ queryKey: ["properties"] });
      queryClient.invalidateQueries({ queryKey: ["property", result.id] });
    },
    onError: (err) => toast.error(extractErrorMessage(err)),
  });
  const needsEnrichment =
    result.relevance_score == null || !result.short_brief;

  return (
    <li className="rounded-md border border-border bg-background p-5 transition-colors hover:border-primary/40">
      <div className="flex items-start gap-4">
        <Thumbnail
          src={result.primary_image_url}
          alt={result.canonical_name}
        />
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <ScoreBadge score={result.relevance_score} />
            <span className="text-xs uppercase text-muted-foreground">
              {result.property_type.replace(/_/g, " ")}
            </span>
            {result.google_rating ? (
              <span className="text-xs text-muted-foreground">
                ⭐ {result.google_rating}
                {result.google_review_count
                  ? ` (${result.google_review_count})`
                  : ""}
              </span>
            ) : null}
            <button
              type="button"
              onClick={() => enrichMutation.mutate()}
              disabled={enrichMutation.isPending}
              title={needsEnrichment ? "Run LLM scoring + brief" : "Re-run LLM scoring + brief"}
              className="ml-auto rounded-md border border-border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
            >
              {enrichMutation.isPending
                ? "Enriching…"
                : needsEnrichment
                ? "Enrich"
                : "Re-enrich"}
            </button>
          </div>

          <Link
            to={`/search/property/${result.id}`}
            className="text-lg font-semibold hover:underline"
          >
            {result.canonical_name}
          </Link>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {result.locality ? `${result.locality}, ` : ""}
            {result.city}
          </div>

          {getListingSpecifics(result).length > 0 ? (
            <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              {getListingSpecifics(result).map((part, idx) => (
                <span key={idx} className="tabular-nums text-foreground">
                  {idx > 0 ? <span className="mr-2 text-muted-foreground">·</span> : null}
                  {part}
                </span>
              ))}
            </div>
          ) : null}

          {result.short_brief ? (
            <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-muted-foreground">
              {result.short_brief}
            </p>
          ) : getScrapedDescription(result) ? (
            // Fallback for external-source rows that haven't been LLM-briefed
            // yet: show the description the scraper extracted (Airbnb /
            // MagicBricks / 99acres all populate this). Click-to-enrich
            // replaces it with a polished LLM brief later.
            <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-muted-foreground">
              {getScrapedDescription(result)}
            </p>
          ) : null}

          {getAmenities(result).length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1">
              {getAmenities(result).slice(0, 6).map((a) => (
                <span
                  key={a}
                  className="rounded-full border border-border bg-muted/30 px-2 py-0.5 text-[11px] text-muted-foreground"
                >
                  {a}
                </span>
              ))}
              {getAmenities(result).length > 6 ? (
                <span className="text-[11px] text-muted-foreground">
                  +{getAmenities(result).length - 6} more
                </span>
              ) : null}
            </div>
          ) : null}

          {result.source_query_text ? (
            <div className="mt-2 text-xs">
              <span className="text-muted-foreground">First seen in </span>
              <Link
                to={`/search/results?q=${encodeURIComponent(result.source_query_text)}`}
                className="rounded-full border border-border bg-muted/40 px-2 py-0.5 font-medium text-foreground hover:bg-muted"
              >
                "{result.source_query_text}"
              </Link>
            </div>
          ) : null}

          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
            {result.canonical_phone ? (
              <a
                href={`tel:${result.canonical_phone}`}
                className="text-primary hover:underline"
              >
                📞 {result.canonical_phone}
              </a>
            ) : null}
            {result.canonical_email ? (
              <a
                href={`mailto:${result.canonical_email}`}
                className="text-primary hover:underline"
              >
                📧 {result.canonical_email}
              </a>
            ) : null}
            {result.canonical_website ? (
              <a
                href={result.canonical_website}
                target="_blank"
                rel="noreferrer"
                className="text-primary hover:underline"
              >
                🌐 website
              </a>
            ) : null}
            {result.external_url ? (
              <a
                href={result.external_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded-full border border-border px-3 py-1 text-xs hover:bg-muted"
              >
                View on {result.source_label ?? "listing"} ↗
              </a>
            ) : null}
          </div>
        </div>
      </div>
    </li>
  );
}

function Thumbnail({ src, alt }: { src: string | null; alt: string }) {
  if (src) {
    return (
      <img
        src={src}
        alt={alt}
        loading="lazy"
        className="h-24 w-24 flex-shrink-0 rounded-md object-cover"
      />
    );
  }
  return (
    <div className="flex h-24 w-24 flex-shrink-0 items-center justify-center rounded-md bg-muted text-xs text-muted-foreground">
      No image
    </div>
  );
}

// Also referenced by SearchHistorySidebar for running-jobs display.
export type { SearchJob };
