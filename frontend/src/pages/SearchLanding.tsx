import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { SearchInput } from "@/components/SearchInput";
import {
  loadSearchPrefs,
  saveSearchPrefs,
  type SearchPrefs,
} from "@/components/SearchOptions";

const EXAMPLES = [
  "resorts in Alibaug",
  "heritage bungalows in Mumbai",
  "boutique hotels in Lonavala",
  "farmhouses in Pune",
];

export function SearchLandingPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [prefs, setPrefs] = useState<SearchPrefs>(() => loadSearchPrefs());

  function handlePrefsChange(next: SearchPrefs) {
    setPrefs(next);
    saveSearchPrefs(next);
  }

  function submit(q: string) {
    const trimmed = q.trim();
    if (trimmed.length < 2) return;
    const params = new URLSearchParams({ q: trimmed });
    navigate(`/search/results?${params.toString()}`);
    queryClient.invalidateQueries({ queryKey: ["search-history"] });
  }

  return (
    <div className="flex min-h-[calc(100vh-8rem)] items-center justify-center">
      <div className="w-full max-w-2xl text-center">
        <h1 className="text-4xl font-semibold tracking-tight">
          Find your next shoot location
        </h1>
        <p className="mx-auto mt-3 max-w-lg text-muted-foreground">
          Search properties across India for filming, weddings, and brand
          shoots. Results include contact details so you can reach out
          directly.
        </p>

        <div className="mt-8">
          <SearchInput
            value={query}
            onChange={setQuery}
            onSubmit={submit}
            prefs={prefs}
            onPrefsChange={handlePrefsChange}
            autoFocus
            size="large"
          />
        </div>

        <div className="mt-6 space-y-2">
          <div className="text-xs text-muted-foreground">Try an example</div>
          <div className="flex flex-wrap justify-center gap-2">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => {
                  setQuery(ex);
                  submit(ex);
                }}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>

        <p className="mt-10 text-xs text-muted-foreground">
          First search for a query takes ~30-60 seconds while we scrape fresh
          data. Cached queries return instantly.
        </p>
      </div>
    </div>
  );
}
