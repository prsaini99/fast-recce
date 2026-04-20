import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { extractErrorMessage } from "@/api/client";
import { propertiesApi } from "@/api/endpoints";
import type { Contact, ReviewAction, ScoreReason } from "@/api/types";
import { PageHeader } from "@/components/AppShell";
import { ScoreBadge, StatusBadge } from "@/components/ScoreBadge";
import { cn } from "@/lib/utils";

export function PropertyDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [notes, setNotes] = useState("");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["property", id],
    queryFn: () => propertiesApi.get(id!),
    enabled: Boolean(id),
  });

  const reviewMutation = useMutation({
    mutationFn: (action: ReviewAction) =>
      propertiesApi.review(id!, {
        action,
        notes: notes || undefined,
      }),
    onSuccess: (res) => {
      toast.success(`Action applied: ${res.status}`);
      queryClient.invalidateQueries({ queryKey: ["property", id] });
      queryClient.invalidateQueries({ queryKey: ["properties"] });
      queryClient.invalidateQueries({ queryKey: ["analytics"] });
      if (res.action_applied === "approve") {
        setTimeout(() => navigate("/outreach"), 800);
      }
    },
    onError: (err) => {
      toast.error(extractErrorMessage(err));
    },
  });

  // Enrichment (scoring + brief) is now on-demand via Gemini. Each button
  // lands directly on its own endpoint so users can pay the LLM cost only
  // for properties they actually care about.
  const enrichMutation = useMutation({
    mutationFn: (kind: "score" | "brief" | "enrich") =>
      kind === "score"
        ? propertiesApi.score(id!)
        : kind === "brief"
        ? propertiesApi.brief(id!)
        : propertiesApi.enrich(id!),
    onSuccess: () => {
      toast.success("Enriched");
      queryClient.invalidateQueries({ queryKey: ["property", id] });
      queryClient.invalidateQueries({ queryKey: ["properties"] });
    },
    onError: (err) => toast.error(extractErrorMessage(err)),
  });

  if (isLoading) {
    return (
      <div className="animate-pulse space-y-4">
        <div className="h-8 w-1/3 rounded bg-muted" />
        <div className="h-20 rounded bg-muted" />
        <div className="h-40 rounded bg-muted" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="rounded-md border border-red-500/40 bg-red-500/10 p-6 text-sm text-red-900 dark:text-red-200">
        Failed to load property: {extractErrorMessage(error)}
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title={data.canonical_name}
        subtitle={
          <>
            {data.locality ? `${data.locality}, ` : ""}
            {data.city}
            {data.state ? `, ${data.state}` : ""}
          </>
        }
        actions={
          <button
            type="button"
            onClick={() => {
              // `document.referrer` is only populated on the very first
              // page load, not on SPA route changes, so we gated the
              // back-navigation on `window.history.length` instead —
              // that's >1 whenever `navigate(-1)` can actually go back.
              if (window.history.length > 1) navigate(-1);
              else navigate("/leads");
            }}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
          >
            ← Back
          </button>
        }
      />

      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <div className="rounded-md border border-border bg-background p-5">
            <div className="mb-3 flex items-center gap-3">
              <ScoreBadge score={data.relevance_score} />
              <StatusBadge status={data.status} />
              {data.google_rating ? (
                <span className="text-xs text-muted-foreground">
                  Google: ⭐ {data.google_rating} ({data.google_review_count} reviews)
                </span>
              ) : null}
            </div>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-sm font-medium text-muted-foreground">
                AI Brief
              </h3>
              <div className="flex gap-2 text-xs">
                <button
                  type="button"
                  onClick={() => enrichMutation.mutate("score")}
                  disabled={enrichMutation.isPending}
                  className="rounded-md border border-border px-2 py-1 hover:bg-muted disabled:opacity-50"
                >
                  {data.relevance_score == null ? "Score" : "Re-score"}
                </button>
                <button
                  type="button"
                  onClick={() => enrichMutation.mutate("brief")}
                  disabled={enrichMutation.isPending}
                  className="rounded-md border border-border px-2 py-1 hover:bg-muted disabled:opacity-50"
                >
                  {data.short_brief ? "Re-brief" : "Generate brief"}
                </button>
                <button
                  type="button"
                  onClick={() => enrichMutation.mutate("enrich")}
                  disabled={enrichMutation.isPending}
                  className="rounded-md border border-primary/50 bg-primary/10 px-2 py-1 font-medium hover:bg-primary/15 disabled:opacity-50"
                >
                  {enrichMutation.isPending ? "Running…" : "Score + brief"}
                </button>
              </div>
            </div>
            <p className="leading-relaxed">
              {data.short_brief ?? (
                <em className="text-muted-foreground">
                  No brief yet — click "Generate brief" to run the LLM.
                </em>
              )}
            </p>
          </div>

          {data.score_reason_json ? (
            <ScoreBreakdown reason={data.score_reason_json} />
          ) : null}

          <ContactList contacts={data.contacts} />

          {data.features_json && Object.keys(data.features_json).length > 0 ? (
            <Features features={data.features_json} />
          ) : null}
        </div>

        <div className="space-y-6">
          <ReviewPanel
            status={data.status}
            notes={notes}
            setNotes={setNotes}
            onAction={(action) => reviewMutation.mutate(action)}
            isPending={reviewMutation.isPending}
          />

          {data.outreach ? <OutreachSummary outreach={data.outreach} /> : null}

          <SourceLinks
            website={data.canonical_website}
            placeId={data.google_place_id}
            lat={data.lat}
            lng={data.lng}
          />
        </div>
      </div>
    </div>
  );
}

function ScoreBreakdown({ reason }: { reason: ScoreReason }) {
  return (
    <div className="rounded-md border border-border bg-background p-5">
      <h3 className="mb-4 text-sm font-medium text-muted-foreground">
        Score breakdown
      </h3>
      <ul className="space-y-2">
        {reason.sub_scores.map((s) => (
          <li key={s.name}>
            <div className="flex items-center justify-between text-sm">
              <span className="capitalize">
                {s.name.replace(/_/g, " ")}{" "}
                <span
                  className={cn(
                    "ml-1 rounded px-1 py-0.5 text-[10px] uppercase tracking-wide",
                    s.source === "llm"
                      ? "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-200"
                      : s.source === "fallback"
                        ? "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-200"
                        : "bg-muted text-muted-foreground"
                  )}
                >
                  {s.source}
                </span>
              </span>
              <span className="tabular-nums text-muted-foreground">
                {s.value.toFixed(2)} × {s.weight.toFixed(2)} ={" "}
                <span className="font-semibold text-foreground">
                  {(s.value * s.weight).toFixed(3)}
                </span>
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded bg-muted">
              <div
                className="h-full rounded bg-primary"
                style={{ width: `${s.value * 100}%` }}
              />
            </div>
            {s.reasoning ? (
              <p className="mt-1 text-xs text-muted-foreground">{s.reasoning}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ContactList({ contacts }: { contacts: Contact[] }) {
  if (contacts.length === 0) {
    return (
      <div className="rounded-md border border-border bg-background p-5">
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">Contacts</h3>
        <p className="text-sm text-muted-foreground">No contacts extracted.</p>
      </div>
    );
  }
  return (
    <div className="rounded-md border border-border bg-background p-5">
      <h3 className="mb-3 text-sm font-medium text-muted-foreground">
        Contacts ({contacts.length})
      </h3>
      <ul className="divide-y divide-border text-sm">
        {contacts.map((c) => (
          <li key={c.id} className="flex items-center gap-3 py-2">
            <span className="w-20 text-xs uppercase text-muted-foreground">
              {c.contact_type}
            </span>
            <span className="flex-1 font-mono">{c.contact_value}</span>
            <span className="text-xs text-muted-foreground">
              {c.extraction_method ?? "—"}
            </span>
            <span className="w-14 text-right text-xs tabular-nums">
              {c.confidence.toFixed(2)}
            </span>
            <div className="flex w-24 justify-end gap-1">
              {c.is_primary ? <Pill tone="green">primary</Pill> : null}
              {c.flagged_personal ? <Pill tone="yellow">personal</Pill> : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Features({ features }: { features: Record<string, unknown> }) {
  const amenities = (features.amenities as string[] | undefined) ?? [];
  const tags = (features.feature_tags as string[] | undefined) ?? [];
  const description = features.description as string | undefined;

  return (
    <div className="rounded-md border border-border bg-background p-5">
      <h3 className="mb-3 text-sm font-medium text-muted-foreground">Features</h3>
      {description ? (
        <p className="mb-3 text-sm leading-relaxed">{description}</p>
      ) : null}
      {amenities.length > 0 ? (
        <div className="mb-2">
          <div className="mb-1 text-xs text-muted-foreground">Amenities</div>
          <div className="flex flex-wrap gap-1">
            {amenities.map((a) => (
              <Pill key={a}>{a}</Pill>
            ))}
          </div>
        </div>
      ) : null}
      {tags.length > 0 ? (
        <div>
          <div className="mb-1 text-xs text-muted-foreground">Feature tags</div>
          <div className="flex flex-wrap gap-1">
            {tags.map((t) => (
              <Pill key={t} tone="indigo">
                {t}
              </Pill>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ReviewPanel({
  status,
  notes,
  setNotes,
  onAction,
  isPending,
}: {
  status: string;
  notes: string;
  setNotes: (v: string) => void;
  onAction: (a: ReviewAction) => void;
  isPending: boolean;
}) {
  const canApprove = ["new", "reviewed", "rejected"].includes(status);
  const canReject = ["new", "reviewed", "approved"].includes(status);
  const canReopen = ["rejected", "do_not_contact"].includes(status);

  return (
    <div className="rounded-md border border-border bg-background p-5">
      <h3 className="mb-2 text-sm font-medium text-muted-foreground">
        Review actions
      </h3>
      <textarea
        placeholder="Add notes (optional)…"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        rows={3}
        className="mb-3 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
      />
      <div className="space-y-2">
        <ActionButton
          disabled={!canApprove || isPending}
          onClick={() => onAction("approve")}
          tone="primary"
        >
          ✅ Approve for outreach
        </ActionButton>
        <ActionButton
          disabled={!canReject || isPending}
          onClick={() => onAction("reject")}
        >
          ❌ Reject
        </ActionButton>
        <ActionButton
          disabled={isPending}
          onClick={() => onAction("do_not_contact")}
          tone="danger"
        >
          🚫 Mark do-not-contact
        </ActionButton>
        {canReopen ? (
          <ActionButton disabled={isPending} onClick={() => onAction("reopen")}>
            ↩ Reopen
          </ActionButton>
        ) : null}
      </div>
    </div>
  );
}

function OutreachSummary({
  outreach,
}: {
  outreach: { id: string; status: string; priority: number; contact_attempts: number; notes: string | null };
}) {
  return (
    <div className="rounded-md border border-border bg-background p-5 text-sm">
      <h3 className="mb-2 text-xs font-medium uppercase text-muted-foreground">
        Outreach
      </h3>
      <div className="flex items-center justify-between">
        <StatusBadge status={outreach.status} />
        <span className="text-xs text-muted-foreground">
          priority {outreach.priority}
        </span>
      </div>
      <div className="mt-2 text-xs text-muted-foreground">
        {outreach.contact_attempts} contact attempt(s)
      </div>
      {outreach.notes ? (
        <p className="mt-2 text-xs">{outreach.notes}</p>
      ) : null}
      <Link
        to="/outreach"
        className="mt-3 block text-xs text-primary hover:underline"
      >
        Manage in outreach pipeline →
      </Link>
    </div>
  );
}

function SourceLinks({
  website,
  placeId,
  lat,
  lng,
}: {
  website: string | null;
  placeId: string | null;
  lat: number | null;
  lng: number | null;
}) {
  return (
    <div className="rounded-md border border-border bg-background p-5 text-sm">
      <h3 className="mb-2 text-xs font-medium uppercase text-muted-foreground">
        Sources
      </h3>
      <ul className="space-y-1">
        {website ? (
          <li>
            🌐{" "}
            <a
              href={website}
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              {website}
            </a>
          </li>
        ) : null}
        {placeId ? (
          <li>
            🗺️{" "}
            <a
              href={`https://www.google.com/maps/place/?q=place_id:${placeId}`}
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              Google Maps place
            </a>
          </li>
        ) : null}
        {lat != null && lng != null ? (
          <li className="text-xs text-muted-foreground">
            {lat.toFixed(4)}, {lng.toFixed(4)}
          </li>
        ) : null}
      </ul>
    </div>
  );
}

function ActionButton({
  children,
  onClick,
  disabled,
  tone,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: "primary" | "danger";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "w-full rounded-md border px-3 py-2 text-sm font-medium transition-colors disabled:opacity-50",
        tone === "primary"
          ? "border-primary bg-primary text-primary-foreground hover:opacity-95"
          : tone === "danger"
            ? "border-red-500/50 text-red-800 hover:bg-red-500/10 dark:text-red-200"
            : "border-border hover:bg-muted"
      )}
    >
      {children}
    </button>
  );
}

function Pill({
  children,
  tone = "default",
}: {
  children: React.ReactNode;
  tone?: "default" | "green" | "yellow" | "indigo";
}) {
  const toneClass = {
    default: "bg-muted text-foreground",
    green: "bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-200",
    yellow: "bg-yellow-100 text-yellow-900 dark:bg-yellow-900/30 dark:text-yellow-200",
    indigo: "bg-indigo-100 text-indigo-900 dark:bg-indigo-900/30 dark:text-indigo-200",
  }[tone];

  return (
    <span className={cn("rounded-md px-2 py-0.5 text-xs", toneClass)}>
      {children}
    </span>
  );
}
