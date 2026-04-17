import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { extractErrorMessage } from "@/api/client";
import { searchApi } from "@/api/endpoints";
import type { SearchResultItem } from "@/api/types";
import { ScoreBadge } from "@/components/ScoreBadge";

const PROGRESS_STAGES = [
  "Searching Google Places…",
  "Searching Airbnb listings…",
  "Fetching place details…",
  "Extracting amenities and photos…",
  "Scoring and writing briefs…",
  "Assembling results…",
];

export function SearchResultsPage() {
  const [params] = useSearchParams();
  const query = params.get("q")?.trim() ?? "";

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["search", query],
    queryFn: () =>
      searchApi.search({
        query,
        max_results: 10,
      }),
    enabled: query.length >= 2,
    staleTime: 5 * 60 * 1000,
    retry: 0,
  });

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="mb-6">
        <div className="text-xs text-muted-foreground">Results for</div>
        <h2 className="text-2xl font-semibold">"{query || "—"}"</h2>
      </div>

      {query.length < 2 ? (
        <p className="text-sm text-muted-foreground">
          Enter a search in the top bar to see results.
        </p>
      ) : isLoading ? (
        <LoadingState />
      ) : isError ? (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 p-4 text-sm">
          Search failed: {extractErrorMessage(error)}
        </div>
      ) : !data || data.results.length === 0 ? (
        <EmptyState errors={data?.errors ?? []} />
      ) : (
        <ResultsList
          results={data.results}
          inferredCity={data.inferred_city}
          duration={data.duration_seconds}
          errors={data.errors}
        />
      )}
    </div>
  );
}

function LoadingState() {
  const [stage, setStage] = useState(0);
  useEffect(() => {
    const id = setInterval(() => {
      setStage((s) => Math.min(s + 1, PROGRESS_STAGES.length - 1));
    }, 8000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-6">
      <div className="rounded-md border border-border bg-background p-6">
        <div className="flex items-center gap-3">
          <div className="h-3 w-3 animate-pulse rounded-full bg-primary" />
          <div className="text-sm font-medium">{PROGRESS_STAGES[stage]}</div>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          Live scraping usually takes 30–60 seconds. Hang tight.
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

// Source label for a result card. Google-Places rows have `source_label=null`;
// we surface them under "Google" so the toggle row stays consistent.
const GOOGLE_LABEL = "Google";

function sourceOf(r: SearchResultItem): string {
  return r.source_label ?? GOOGLE_LABEL;
}

// localStorage key — shared across searches so the user's preference persists.
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
    // quota / disabled storage — silent, the filter still works in-memory.
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
  const [hiddenSources, setHiddenSources] = useState<Set<string>>(() =>
    loadHiddenSources(),
  );

  // Counts per source in the current result batch — used to (a) label
  // the pills with "(n)" and (b) auto-hide pills for sources that
  // produced zero cards (so commercial searches don't show dead toggles).
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

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          {visibleResults.length} of {results.length} result(s)
          {inferredCity ? <> in <strong>{inferredCity}</strong></> : null}
          · scraped in {duration.toFixed(1)}s
        </span>
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

function ResultCard({ result }: { result: SearchResultItem }) {
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

          {result.short_brief ? (
            <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-muted-foreground">
              {result.short_brief}
            </p>
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
  // Initials placeholder so the card layout doesn't shift between rows.
  const initials = alt
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("") || "?";
  return (
    <div className="flex h-24 w-24 flex-shrink-0 items-center justify-center rounded-md bg-muted text-base font-semibold text-muted-foreground">
      {initials}
    </div>
  );
}
