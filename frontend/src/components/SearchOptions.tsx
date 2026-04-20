import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * Per-user search-time overrides. `null` means "defer to server-side
 * default" so a fresh user gets whatever the admin configured. Once
 * they toggle anything we remember the chosen value explicitly.
 */
export interface SearchPrefs {
  max_results: number;
  use_airbnb: boolean | null;
  use_magicbricks: boolean | null;
  use_acres99: boolean | null;
}

export const DEFAULT_PREFS: SearchPrefs = {
  max_results: 10,
  use_airbnb: null,
  use_magicbricks: null,
  use_acres99: null,
};

const STORAGE_KEY = "fastrecce.search.prefs.v1";

export function loadSearchPrefs(): SearchPrefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PREFS;
    const parsed = JSON.parse(raw) as Partial<SearchPrefs>;
    return { ...DEFAULT_PREFS, ...parsed };
  } catch {
    return DEFAULT_PREFS;
  }
}

export function saveSearchPrefs(prefs: SearchPrefs): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // quota / disabled — non-fatal, in-memory prefs still apply.
  }
}

interface Props {
  prefs: SearchPrefs;
  onChange: (prefs: SearchPrefs) => void;
}

export function SearchOptionsButton({ prefs, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  // Indicator count — how many non-default choices the user has made.
  const customCount =
    (prefs.max_results !== DEFAULT_PREFS.max_results ? 1 : 0) +
    (prefs.use_airbnb !== null ? 1 : 0) +
    (prefs.use_magicbricks !== null ? 1 : 0) +
    (prefs.use_acres99 !== null ? 1 : 0);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Search options"
        title="Search options"
        className={cn(
          "flex h-9 shrink-0 items-center gap-1.5 rounded-md border border-border bg-background px-3 text-sm transition-colors hover:bg-muted",
          customCount > 0 && "border-primary/40 bg-primary/5",
        )}
      >
        <span aria-hidden>⚙︎</span>
        <span>Options</span>
        {customCount > 0 ? (
          <span className="rounded-full bg-primary/20 px-1.5 text-xs">
            {customCount}
          </span>
        ) : null}
      </button>

      {open ? (
        <div
          role="dialog"
          className="absolute right-0 top-full z-30 mt-1 w-72 rounded-md border border-border bg-background p-4 text-sm shadow-lg"
        >
          <div className="mb-3 flex items-center justify-between">
            <div className="font-medium">Search options</div>
            <button
              type="button"
              onClick={() => onChange(DEFAULT_PREFS)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              Reset
            </button>
          </div>

          <label className="mb-4 block">
            <span className="mb-1 block text-xs text-muted-foreground">
              Max results ({prefs.max_results})
            </span>
            <input
              type="range"
              min={1}
              max={30}
              step={1}
              value={prefs.max_results}
              onChange={(e) =>
                onChange({ ...prefs, max_results: Number(e.target.value) })
              }
              className="w-full"
            />
          </label>

          <div className="mb-1 text-xs text-muted-foreground">
            Property scrapers
          </div>
          <p className="mb-2 text-[11px] text-muted-foreground">
            Only fire when the query is about properties (residential or
            generic intent).
          </p>
          <ScraperToggle
            label="Airbnb"
            value={prefs.use_airbnb}
            onChange={(v) => onChange({ ...prefs, use_airbnb: v })}
          />
          <ScraperToggle
            label="MagicBricks"
            value={prefs.use_magicbricks}
            onChange={(v) => onChange({ ...prefs, use_magicbricks: v })}
          />
          <ScraperToggle
            label="99acres"
            value={prefs.use_acres99}
            onChange={(v) => onChange({ ...prefs, use_acres99: v })}
          />

          <p className="mt-3 text-[11px] text-muted-foreground">
            "Default" defers to the server's env flag.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function ScraperToggle({
  label,
  value,
  onChange,
}: {
  label: string;
  value: boolean | null;
  onChange: (v: boolean | null) => void;
}) {
  const options: { key: string; label: string; val: boolean | null }[] = [
    { key: "default", label: "Default", val: null },
    { key: "on", label: "On", val: true },
    { key: "off", label: "Off", val: false },
  ];
  return (
    <div className="mb-2 flex items-center justify-between">
      <span>{label}</span>
      <div className="flex overflow-hidden rounded-md border border-border text-xs">
        {options.map((opt) => {
          const active = value === opt.val;
          return (
            <button
              key={opt.key}
              type="button"
              onClick={() => onChange(opt.val)}
              className={cn(
                "px-2 py-1 transition-colors",
                active
                  ? "bg-primary text-primary-foreground"
                  : "bg-background hover:bg-muted",
              )}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
