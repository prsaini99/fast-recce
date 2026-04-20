import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { SearchHistorySidebar } from "@/components/SearchHistorySidebar";
import { SearchInput } from "@/components/SearchInput";
import {
  loadSearchPrefs,
  saveSearchPrefs,
  type SearchPrefs,
} from "@/components/SearchOptions";
import { cn } from "@/lib/utils";

interface NavItem {
  label: string;
  to: string;
}

const NAV: NavItem[] = [
  { label: "Search", to: "/search" },
  { label: "Leads", to: "/leads" },
  { label: "Analytics", to: "/analytics" },
];

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const initialQuery = new URLSearchParams(location.search).get("q") ?? "";
  const [query, setQuery] = useState(initialQuery);
  const [prefs, setPrefs] = useState<SearchPrefs>(() => loadSearchPrefs());

  const onSearchPage = location.pathname.startsWith("/search");
  const showCompactSearch = location.pathname.startsWith("/search/results");

  function handlePrefsChange(next: SearchPrefs) {
    setPrefs(next);
    saveSearchPrefs(next);
  }

  useEffect(() => {
    // Keep the top-bar input synced with the URL when navigating between
    // past searches from the sidebar.
    setQuery(new URLSearchParams(location.search).get("q") ?? "");
  }, [location.search]);

  function submitSearch(v: string) {
    const params = new URLSearchParams();
    params.set("q", v);
    navigate(`/search/results?${params.toString()}`);
    // Sidebar may have a new entry after a live search; refresh it.
    queryClient.invalidateQueries({ queryKey: ["search-history"] });
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-background">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-6 py-3">
          <Link to="/search" className="shrink-0 text-lg font-semibold">
            FastRecce
          </Link>

          <nav className="flex items-center gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-1.5 text-sm transition-colors",
                    isActive
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground"
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          {showCompactSearch ? (
            <SearchInput
              value={query}
              onChange={setQuery}
              onSubmit={submitSearch}
              prefs={prefs}
              onPrefsChange={handlePrefsChange}
            />
          ) : (
            <div className="flex-1" />
          )}
        </div>
      </header>

      <div className="mx-auto flex max-w-7xl">
        {onSearchPage ? <SearchHistorySidebar /> : null}
        <main className="min-w-0 flex-1 px-6 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex items-start justify-between gap-4">
      <div>
        <h2 className="text-2xl font-semibold">{title}</h2>
        {subtitle ? (
          <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
        ) : null}
      </div>
      {actions ? <div className="flex gap-2">{actions}</div> : null}
    </header>
  );
}
