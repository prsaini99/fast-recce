import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { searchApi } from "@/api/endpoints";
import {
  SearchOptionsButton,
  type SearchPrefs,
} from "@/components/SearchOptions";
import { cn } from "@/lib/utils";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSubmit: (v: string) => void;
  prefs?: SearchPrefs;
  onPrefsChange?: (prefs: SearchPrefs) => void;
  placeholder?: string;
  autoFocus?: boolean;
  size?: "default" | "large";
}

/**
 * Search input with typeahead suggestions from past searches.
 *
 * Suggestions are fetched only while the input is focused and the
 * current query is at least 2 characters. Clicking a suggestion
 * submits immediately — past searches replay from the backend cache.
 */
export function SearchInput({
  value,
  onChange,
  onSubmit,
  prefs,
  onPrefsChange,
  placeholder = "e.g. resorts in Alibaug",
  autoFocus,
  size = "default",
}: Props) {
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  const trimmed = value.trim();
  const enabled = focused && trimmed.length >= 2;

  const { data: suggestions } = useQuery({
    queryKey: ["search-suggest", trimmed],
    queryFn: () => searchApi.suggest(trimmed),
    enabled,
    staleTime: 30_000,
    retry: 0,
  });

  useEffect(() => {
    setActiveIndex(-1);
  }, [trimmed]);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (!containerRef.current?.contains(e.target as Node)) setFocused(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const showDropdown = focused && (suggestions?.length ?? 0) > 0;

  function commit(v: string) {
    const clean = v.trim();
    if (clean.length < 2) return;
    onChange(clean);
    onSubmit(clean);
    setFocused(false);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!showDropdown) return;
    const items = suggestions ?? [];
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(items.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(-1, i - 1));
    } else if (e.key === "Enter") {
      if (activeIndex >= 0 && items[activeIndex]) {
        e.preventDefault();
        commit(items[activeIndex].query_text);
      }
    } else if (e.key === "Escape") {
      setFocused(false);
    }
  }

  const inputCls = cn(
    "w-full rounded-md border border-border bg-background outline-none focus:ring-2 focus:ring-primary",
    size === "large" ? "px-4 py-3 text-base" : "px-3 py-1.5 text-sm"
  );

  return (
    <div ref={containerRef} className="relative flex-1">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          commit(value);
        }}
        className="flex items-center gap-2"
      >
        <input
          autoFocus={autoFocus}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => setFocused(true)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          role="combobox"
          aria-expanded={showDropdown}
          aria-controls={listboxId}
          aria-autocomplete="list"
          className={inputCls}
        />
        {prefs && onPrefsChange ? (
          <SearchOptionsButton prefs={prefs} onChange={onPrefsChange} />
        ) : null}
        <button
          type="submit"
          className={cn(
            "rounded-md bg-primary font-medium text-primary-foreground hover:opacity-95",
            size === "large" ? "px-5 py-3 text-base" : "px-3 py-1.5 text-sm"
          )}
        >
          Search
        </button>
      </form>

      {showDropdown ? (
        <ul
          id={listboxId}
          role="listbox"
          className="absolute left-0 right-16 top-full z-20 mt-1 max-h-80 overflow-auto rounded-md border border-border bg-background shadow-lg"
        >
          {(suggestions ?? []).map((s, idx) => (
            <li
              key={s.query_text}
              role="option"
              aria-selected={idx === activeIndex}
              onMouseDown={(e) => {
                // preventDefault so blur doesn't fire before click.
                e.preventDefault();
                commit(s.query_text);
              }}
              onMouseEnter={() => setActiveIndex(idx)}
              className={cn(
                "flex cursor-pointer items-center justify-between gap-3 px-3 py-2 text-sm",
                idx === activeIndex ? "bg-muted" : "hover:bg-muted/60"
              )}
            >
              <span className="truncate">{s.query_text}</span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {s.result_count} result{s.result_count === 1 ? "" : "s"} · cached
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
