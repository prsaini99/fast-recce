import type { Contact, ScoreReason } from "@/api/types";
import { cn } from "@/lib/utils";

/**
 * Read-only property subviews shared by the admin PropertyDetail page
 * and the public PublicPropertyDetail page.
 */

export function ScoreBreakdown({ reason }: { reason: ScoreReason }) {
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

export function ContactList({ contacts }: { contacts: Contact[] }) {
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

export function Features({ features }: { features: Record<string, unknown> }) {
  const amenities = (features.amenities as string[] | undefined) ?? [];
  const tags = (features.feature_tags as string[] | undefined) ?? [];
  const description = features.description as string | undefined;

  if (!description && amenities.length === 0 && tags.length === 0) return null;

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

const _SOURCE_LABELS: Record<string, string> = {
  airbnb: "Airbnb",
  magicbricks: "MagicBricks",
  "99acres": "99acres",
};

export function SourceLinks({
  website,
  placeId,
  lat,
  lng,
  externalUrl,
}: {
  website: string | null;
  placeId: string | null;
  lat: number | null;
  lng: number | null;
  externalUrl?: string | null;
}) {
  // `google_place_id` is overloaded: for Airbnb / MagicBricks / 99acres rows
  // it's actually `<source>:<listing_id>`. Detect the prefix so we render a
  // source-appropriate link instead of pointing an Airbnb listing at Google
  // Maps.
  const prefix = (placeId ?? "").split(":", 1)[0];
  const externalLabel = _SOURCE_LABELS[prefix];
  const isExternalSource = Boolean(externalLabel);

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
        {isExternalSource && externalUrl ? (
          <li>
            🔗{" "}
            <a
              href={externalUrl}
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              View on {externalLabel}
            </a>
          </li>
        ) : null}
        {!isExternalSource && placeId ? (
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

export function Pill({
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
