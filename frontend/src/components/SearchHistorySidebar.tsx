import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { toast } from "sonner";

import { extractErrorMessage } from "@/api/client";
import { searchApi } from "@/api/endpoints";
import type { SearchJob } from "@/api/types";
import { cn } from "@/lib/utils";

/**
 * Left rail showing:
 *   1. currently-running jobs (polled every 2s so completion auto-reflects)
 *   2. past completed searches from search_history
 *
 * A running job becomes clickable once complete; while running, clicking
 * the item still navigates to its results page where the poll takes over.
 */
export function SearchHistorySidebar() {
  const location = useLocation();
  const queryClient = useQueryClient();
  const currentQuery = new URLSearchParams(location.search).get("q") ?? "";

  const runningJobs = useQuery({
    queryKey: ["search-jobs", "running"],
    queryFn: () => searchApi.listJobs("running"),
    refetchInterval: 2000,
    staleTime: 0,
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => searchApi.cancelJob(id),
    onSuccess: () => {
      toast.success("Job cancelled");
      queryClient.invalidateQueries({ queryKey: ["search-jobs", "running"] });
    },
    onError: (err) => toast.error(extractErrorMessage(err)),
  });

  // When a running job flips to completed, invalidate the history feed so
  // the new entry slides into the "Past searches" list.
  const runningCount = runningJobs.data?.length ?? 0;
  const [lastRunningCount, setLastRunningCount] = useState(runningCount);
  useEffect(() => {
    if (runningCount < lastRunningCount) {
      queryClient.invalidateQueries({ queryKey: ["search-history"] });
    }
    setLastRunningCount(runningCount);
  }, [runningCount, lastRunningCount, queryClient]);

  const history = useQuery({
    queryKey: ["search-history"],
    queryFn: () => searchApi.history(50),
    staleTime: 10_000,
  });

  return (
    <aside className="hidden w-64 shrink-0 border-r border-border bg-muted/20 lg:block">
      <div className="sticky top-0 flex h-full max-h-screen flex-col">
        {runningCount > 0 ? (
          <section className="border-b border-border px-2 py-3">
            <div className="px-2 pb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Running ({runningCount})
            </div>
            <ul className="space-y-0.5">
              {runningJobs.data?.map((job) => (
                <RunningJobItem
                  key={job.id}
                  job={job}
                  active={currentQuery.toLowerCase() === job.query_text.toLowerCase()}
                  onCancel={(id) => cancelMutation.mutate(id)}
                  canceling={cancelMutation.isPending && cancelMutation.variables === job.id}
                />
              ))}
            </ul>
          </section>
        ) : null}

        <div className="border-b border-border px-4 py-3">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Past searches
          </div>
        </div>

        <div className="flex-1 overflow-y-auto py-2">
          {history.isLoading ? (
            <div className="space-y-1 px-3">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="h-10 animate-pulse rounded bg-muted/50" />
              ))}
            </div>
          ) : !history.data || history.data.length === 0 ? (
            <div className="px-4 py-6 text-xs text-muted-foreground">
              No searches yet. Try something like "resorts in Alibaug".
            </div>
          ) : (
            <ul className="space-y-0.5 px-2">
              {history.data.map((item) => {
                const isActive =
                  currentQuery.toLowerCase() === item.query_text.toLowerCase();
                return (
                  <li key={item.id}>
                    <Link
                      to={`/search/results?q=${encodeURIComponent(item.query_text)}`}
                      className={cn(
                        "flex flex-col gap-0.5 rounded-md px-2 py-2 text-sm transition-colors",
                        isActive
                          ? "bg-primary/10 text-foreground"
                          : "text-foreground hover:bg-muted"
                      )}
                    >
                      <span className="line-clamp-2 break-words">{item.query_text}</span>
                      <span className="flex items-center gap-2 text-[11px] text-muted-foreground">
                        <span>{item.result_count} result{item.result_count === 1 ? "" : "s"}</span>
                        <span>·</span>
                        <span>{formatRelative(item.last_searched_at)}</span>
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </aside>
  );
}

function RunningJobItem({
  job,
  active,
  onCancel,
  canceling,
}: {
  job: SearchJob;
  active: boolean;
  onCancel: (id: string) => void;
  canceling: boolean;
}) {
  const [seconds, setSeconds] = useState(() =>
    elapsedSeconds(job.started_at),
  );
  useEffect(() => {
    const id = setInterval(() => setSeconds(elapsedSeconds(job.started_at)), 500);
    return () => clearInterval(id);
  }, [job.started_at]);

  return (
    <li
      className={cn(
        "group flex items-center gap-2 rounded-md pr-1 transition-colors",
        active
          ? "bg-primary/10"
          : "hover:bg-muted",
      )}
    >
      <Link
        to={`/search/results?q=${encodeURIComponent(job.query_text)}`}
        className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-2 text-sm text-foreground"
      >
        <span
          aria-hidden
          className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-primary"
        />
        <span className="min-w-0 flex-1 truncate">{job.query_text}</span>
        <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
          {seconds}s
        </span>
      </Link>
      <button
        type="button"
        disabled={canceling}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onCancel(job.id);
        }}
        aria-label="Cancel job"
        title="Cancel job"
        className="shrink-0 rounded-md px-1.5 py-1 text-xs text-muted-foreground opacity-0 transition-opacity hover:bg-background hover:text-foreground group-hover:opacity-100 disabled:opacity-40"
      >
        ✕
      </button>
    </li>
  );
}

function elapsedSeconds(iso: string): number {
  const started = new Date(iso).getTime();
  return Math.max(0, Math.floor((Date.now() - started) / 1000));
}

function formatRelative(iso: string): string {
  const d = new Date(iso);
  const diffMs = Date.now() - d.getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString();
}
